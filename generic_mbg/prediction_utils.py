# Copyright (C) 2009 Anand Patil
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


import pymc as pm
import numpy as np
from map_utils import import_raster, export_raster
from scipy import ndimage, mgrid
from histogram_utils import *
from inference_utils import invlogit, fast_inplace_mul, fast_inplace_square, crossmul_and_sum, CovarianceWithCovariates
import time
import os
import warnings
import copy
import decimal

memmax = 2.5e8

def chains(hf):
    return [gr for gr in hf.listNodes("/") if gr._v_name[:5]=='chain']

def all_chain_len(hf):
    return np.sum([len(chain.PyMCsamples) for chain in chains(hf)])
    
def all_chain_trace(hf, name):
    return np.concatenate([np.ravel(chain.PyMCsamples.col(name)) for chain in chains(hf)])

def time_msg(time_count, k, iter, time_start):
    if time.time() - time_count > 10:
        print ((k*100)/len(iter)), '% complete',
        time_count = time.time()      
        if k > 0:      
            print 'expect results %s (in %s hours)'%(time.ctime((time_count-time_start)*len(iter)/float(k)+time_start),(time_count-time_start)*(len(iter)-float(k))/float(k)/3600)
        else:
            print
    return time.time()

def plot_variables(M):
    import pylab as pl
    for s in M._variables_to_tally:
        if np.prod(np.shape(s.value)) == 1 and np.asarray(s.value).dtype != np.dtype('object'):
            print 'Plotting %s'%s.__name__
            pl.clf()
            pl.plot(all_chain_trace(M.db._h5file, s.__name__))
            pl.title(s.__name__)
            pl.savefig(s.__name__+'.pdf')

def draw_points_from_weight(n_points, geom, weight, weight_lon, weight_lat):
    import shapely
    in_geom = [geom.contains(shapely.geometry.Point(lon,lat)) for lon,lat in zip(weight_lon, weight_lat)]
    weights_in_geom = weight[np.where(in_geom)].astype('float')
    weights_in_geom /= weights_in_geom.sum()

    choices = pm.rcategorical(weights_in_geom, size=n_points)
    return weight_lon[choices], weight_lat[choices]
    

def draw_points(geoms, n_points, weight=None, weight_lon=None, weight_lat=None, tlims=None, coordinate_time=False, n_time_points=None):
    
    areas = [g.area for g in geoms]
    areas = np.array(areas)/np.sum(areas)

    if coordinate_time and tlims:
        n_spatial_points = np.round(np.float(n_points)/n_time_points)
    else:
        n_spatial_points = n_points

    n_per = [max(1,np.round(n_spatial_points*a)) for a in areas]
    
    if tlims:
        if coordinate_time:
            t = [np.linspace(tlim[0],tlim[1],n_time_points) for tlim in tlims]
        else:
            t = [np.random.uniform(tlim[0],tlim[1], size=np_) for np_,tlim in zip(n_per, tlims)]
    
    if weight:
        lonlat = [draw_points_from_weight(np_, g, weight, weight_lon, weight_lat) for np_,g in zip(n_per, geoms)]
    else:
        import map_utils
        lonlat = [map_utils.multipoly_sample(np_, g) for np_,g in zip(n_per, geoms)]

    if tlims:
        return [np.vstack((l[0],l[1],t_)).T for l,t in zip(lonlat, times)]
    else:
        return [np.vstack((l[0],l[1])).T for l in lonlat]
    
def all_chain_getitem(hf, name, i, vl=False):
    c = chains(hf)
    lens = [len(chain.PyMCsamples) for chain in c]
    if i >= np.sum(lens):
        raise IndexError, 'Index out of bounds'
    s = 0
    j = i

    for k in xrange(len(lens)):
        s += lens[k]
        if i<s:
            if vl:
                return getattr(c[k].group0, name)[j]
            else:
                return getattr(c[k].PyMCsamples.cols,name)[j]
        else:
            j -= lens[k]
            
def all_chain_remember(M, i):
    c = chains(M.db._h5file)
    lens = [len(chain.PyMCsamples) for chain in c]
    if i >= np.sum(lens):
        raise IndexError, 'Index out of bounds'
    s = 0
    j = i

    for k in xrange(len(lens)):
        s += lens[k]
        if i<s:
            M.remember(k,j)
        else:
            j -= lens[k]
    
def validate_format_str(st):
    for i in [0,2]:
        if not st[i] in ['x','y']:
            raise ValueError, 'Directions must be x or y'
    for j in [1,3]:
        if not st[j] in ['-', '+']:
            raise ValueError, 'Orders must be + or -'
            
    if st[0]==st[2]:
        raise ValueError, 'Directions must be different'
    
    
def grid_convert(g, frm, to, validate=False):
    """Converts a grid to a new layout.
      - g : 2d array
      - frm : format string
      - to : format string
      
      Example format strings:
        - x+y+ (the way Anand does it) means that 
            - g[i+1,j] is west of g[i,j]
            - g[i,j+1] is north of g[i,j]
        - y-x+ (map view) means that 
            - g[i+1,j] is south of g[i,j]
            - g[i,j+1] is west of g[i,j]"""
    
    # Validate format strings
    if validate:
        for st in [frm, to]:
            validate_format_str(st)
        
    # Transpose if necessary
    if not frm[0]==to[0]:
        g = g.T
                
    first_dir = to[1]
    if not first_dir == frm[frm.find(to[0])+1]:
        g=g[::-1,:]
        
    sec_dir = to[3]
    if not sec_dir == frm[frm.find(to[2])+1]:
        g=g[:,::-1]
        
    # print first_dir, sec_dir
    return g

def get_circle(n):
    cover = np.arange(-n,n+1)
    coversq = np.dstack(np.meshgrid(cover,cover))
    covercirc = np.where(np.sum(coversq**2, axis=-1)<=n**2)
    out =  coversq[covercirc]
    return out

def buffer(arr, n=5):
    """Creates an n-pixel buffer in all directions."""
    arr = np.asarray(arr,order='F',dtype='bool')
    if n > 0:
        circ_ind = get_circle(n)
        out = bufster(arr, circ_ind)
    else:
        out = arr.copy('F')
    return out.astype('bool')

def raster_to_locs(name, path='.', thin=1, bufsize=1):
    """Converts a raster grid to a list of locations where prediction is desired."""
    lon,lat,data,type = import_raster(name,path)
    data = grid_convert(data,'y-x+','x+y+')
    unmasked = buffer(True-data[::thin,::thin].mask, n=bufsize)
    
    # unmasked = None
    lat,lon = [dir[unmasked] for dir in np.meshgrid(lat[::thin],lon[::thin])]
    if np.any(np.abs(lon)>180.) or np.any(np.abs(lat)>90.):
        raise ValueError, 'Ascii file %s has incorrect header. Lower left corner is (%f,%f); upper right corner is (%f,%f).'%(fname,lat.min(),lon.min(),lat.max(),lon.max())
    # lon,lat = [dir.ravel() for dir in np.meshgrid(lon[::thin],lat[::thin])]
    return np.vstack((lon,lat)).T*np.pi/180., unmasked, type

def raster_to_vals(name, path='.', thin=1, unmasked=None):
    """
    Converts a raster grid to a list of values where prediction is desired.
    If the unmasked argument is provided, the mask of the ascii grid is
    checked against that mask.
    """
    lon,lat,data,type = import_raster(name, path)
    data = grid_convert(data,'y-x+','x+y+')    
    if unmasked is not None:
        input_unmasked = True-data[::thin,::thin].mask
        if not np.all(unmasked == input_unmasked):
            if unmasked.shape == input_unmasked.shape:
                where_mismatch = np.where(input_unmasked != unmasked)
                import pylab as pl
                pl.clf()
                pl.plot(lon[where_mismatch[0]],lat[where_mismatch[1]],'k.',markersize=2)
                pl.savefig('mismatch.pdf')
                msg = '%s: covariate raster\'s mask does not match mask at the following pixels (in decimal degrees):\n'%name
                for i,j in zip(*where_mismatch):
                    msg += "\t%f, %f\n"%(lon[i],lat[j])
                msg += 'Image of mismatched points saved as mismatch.pdf'
            else:
                msg = '%s: covariate raster is not same shape as mask.'%name
            raise ValueError, msg
    
    return data.data[::thin,::thin][unmasked]

def get_mask_t(o, hf):
    x, unmasked, output_type = raster_to_locs(o.mask_name, thin=o.raster_thin, bufsize=o.bufsize, path=o.raster_path)
    if o.year is None:
        if 't' in hf.root.input_csv.colnames:
            raise ValueError, 'No year provided, but the model appears to be temporal.'
    else:
        x = np.vstack((x.T,o.year*np.ones(x.shape[0]))).T
    return unmasked, x
        
def display_datapoints(h5file, path='', cmap=None, *args, **kwargs):
    """Adds as hdf5 archive's logp-mesh to an image."""
    import tables as tb
    
    hf = tb.openFile(path+h5file)
    lpm = hf.root.metadata.logp_mesh[:]
    hf.close()
    from pylab import plot
    if cmap is None:
        plot(lpm[:,0],lpm[:,1],*args,**kwargs)
        
def mean_reduce(sofar, next, name):
    """A function to be used with hdf5_to_samps"""
    if sofar is None:
        return next
    else:
        return sofar + next
        
def var_reduce(sofar, next, name):
    """A function to be used with hdf5_to_samps"""
    if sofar is None:
        return next**2
    else:
        return sofar + next**2
        
def moments_finalize(prod, n):
    """Finalizes accumulated moments in human-interpreted surfaces."""
    mean = prod[mean_reduce] / n
    var = prod[var_reduce] / n - mean**2
    std = np.sqrt(var)
    std_to_mean = std/mean
    out = {'mean': mean, 'var': var, 'std': std, 'std-to-mean':std_to_mean}
    return out
        
def sample_reduce(sofar, next, name):
    """A function to be used with hdf5_to_samps. Keeps all samples with no data loss."""
    if sofar is None:
        return [next]
    else:
        sofar.append(next)
        return sofar
        
def sample_finalize(prod, n):
    """Converts accumulated samples to an array for output."""
    return {'samples': np.array(prod[sample_reduce])}

def histogram_reduce(bins, binfn):
    """Produces an accumulator to be used with hdf5_to_samps"""
    def hr(sofar, next, name):
        next = np.atleast_1d(next)
        if sofar is None:
            sofar = np.zeros(next.shape+(len(bins),), dtype=int, order='F')
        # Call to Fortran function multiinc
        ind = binfn(next)
        multiinc(sofar,ind)
        
        return sofar
    return hr
        
def histogram_finalize(bins, q, hr, ci=None):
    """Converts accumulated histogram raster to desired quantiles"""
    def fin(products, n, bins=bins, q=q, hr=hr, ci=ci):
        out = {}
        hist = products[hr]
        # Call to Fortran function qextract
        quantile_surfs = qextract(hist,n,q,bins)
        for i in xrange(len(q)):
            out['quantile-%s'%q[i]] = quantile_surfs[i]
        if ci:
            for cii in ci:
                q_lo = (decimal.Decimal(1)-cii)*decimal.Decimal('0.5')
                q_hi = decimal.Decimal(1)-q_lo
                out['ci-%s'%cii] = out['quantile-%s'%q_hi]-out['quantile-%s'%q_lo]
        return out
    return fin    

def get_one_args(postproc, f_labels, M):
    import inspect
    try:
        argspec = inspect.getargspec(postproc)
    except:
        argspec = inspect.getargspec(postproc.__call__)
        argspec.args.remove('self')
    args = argspec.args
    if argspec.defaults is None:
        postproc_args = args
    else:
        required_args = args[:-len(argspec.defaults)]
        optional_args = filter(lambda k, M=M: hasattr(M,k), args[-len(argspec.defaults):])
        postproc_args = required_args+optional_args
    extra_postproc_args = set(postproc_args) - set(f_labels)
    return postproc_args, extra_postproc_args
    
def get_args(postprocs, fns, f_labels, M):
    # Have a look at the postprocessing functions
    products = {}
    postproc_args = {}
    extra_postproc_args = {}
    for postproc in postprocs:
        products[postproc] = dict(zip(fns, [None]*len(fns)))
        postproc_args[postproc], extra_postproc_args[postproc] = get_one_args(postproc,f_labels,M)
    return products, postproc_args, extra_postproc_args

def apply_postprocs_and_reduce(M, n_per, M_preds, S_preds, postprocs, fns, products, postproc_args, extra_postproc_args, joint, norms=None, **kwds):
    # Postprocess if necessary: logit, etc.
    if norms is None:
        norms = np.random.normal(size=n_per)
    gp_submods = M_preds.keys()
    
    cmin, cmax = pm.thread_partition_array(M_preds[gp_submods[0]])

    for j in xrange(n_per):
    
        postproc_kwds = {}
        # Evaluate fields, and store them in argument dict for postproc
        for s in gp_submods:
            if joint:
                postproc_kwds[s.name] = pm.rmv_normal_chol(M_preds[s], S_preds[s])
            else:
                postproc_kwds[s.name] = M_preds[s].copy('F')
                pm.map_noreturn(iaaxpy, [(norms[j], S_preds[s], postproc_kwds[s.name], cmin[l], cmax[l]) for l in xrange(len(cmax))])
    
        for postproc in postprocs:
            postproc_kwds_ = copy.copy(postproc_kwds)
            # Pull any extra variables needed for postprocessing out of trace
            for extra_arg in extra_postproc_args[postproc]:
                postproc_kwds_[extra_arg] = pm.utils.value(getattr(M, extra_arg))

            # Evaluate postprocessing function to get a surface
            surf = postproc(**postproc_kwds_)
    
            # Reduce surface into products needed
            for f in fns:
                products[postproc][f] = f(products[postproc][f], surf, postproc.__name__, **kwds)

def hdf5_to_areal_samps(M, x, nuggets, burn, thin, total, fns, h, g, pred_covariate_dict, finalize=None, continue_past_npd=False):
    """
    Parameters:
        M : MCMC object
            Its database should have samples in it.
        x : dict of dicts of arrays
            {geomname: {geomname: [lon, lat]}}
        nuggets : dict
            Should map the GP submodels of M to the corresponding nugget values,
            if any.
        burn : int
            Burnin iterations to discard.
        thin : int
            Number of iterations between ones that get used in the predictions.
        total : int
            Total number of iterations to use in thinning.
        fns : list of functions
            Each function should take four arguments: sofar, next, cols and i.
            Sofar may be None.
            The functions will be applied according to the reduce pattern.
        h : dict of dicts of functions
            {postproc: geomcoll: lambda integral1, integral2, integral3, ... : return}
        g : dict of dict of functions
            {postproc: geomcoll: geomcoll: lambda fs, [lon, lat, t], variables : return.}
        pred_covariate_dict : dict of dict of dicts of arrays
            {geomcoll: geomcoll: covariate: [val]}.
        finalize : function (optional)
            This function is applied to the products before returning. It should
            take a second argument which is the actual number of realizations
            produced.
            
    Output is a dictionary of form {postproc: {geomcoll: {fn: value}}}
    """    
    hf=M.db._h5file
    gp_submods = list(set(filter(lambda c: isinstance(c,pm.gp.GPSubmodel), M.containers)))
    f_labels = [gps.name for gps in gp_submods]
    
    postprocs = h.keys()

    # Figure out the function signatures of h and g
    products = {}
    h_args = {}
    extra_h_args = {}
    g_args = {}
    extra_g_args = {}
    for outercoll in x.iterkeys():
        products[outercoll] = {}
        h_args[outercoll] = {}
        extra_h_args[outercoll] = {}
        for postproc in postprocs:
            h_ = h[postproc][outercoll]
            g_keys = g[postproc][outercoll].keys()
            p, a, ea = get_args([h_], fns, g_keys, M)
            products[outercoll][postproc], h_args[outercoll][postproc], extra_h_args[outercoll][postproc] = p[h_], a[h_], ea[h_]
            for g_ in g[postproc][outercoll].itervalues():
                g_args[g_], extra_g_args[g_] = get_one_args(g_, f_labels+['x'], M)

    iter = np.arange(burn,all_chain_len(hf),thin)

    if len(iter)==0:
        raise ValueError, 'You asked for %i burnin iterations with thinnnig %i but the chains are only %i iterations long.'%(burn, thin, all_chain_len(hf))
    if (total>len(iter)):
        n_per = total/len(iter)+1

    if (total<=len(iter)):
        n_per=1
        newthin=(all_chain_len(hf)-burn)/total
        iter = np.arange(burn,all_chain_len(hf),newthin)

    if len(iter)==0:
        raise ValueError, 'Check --total --thin and burn parameters, currently asking for zero realisations'        
    actual_total = n_per * len(iter)
    print("total chain length = "+str(all_chain_len(hf))+"\nburn = "+str(burn)+"\nthin = "+str(thin)+"\ntotal = "+str(total))
    print("will do "+str(len(iter))+" full iterations with "+str(n_per)+" nuggets each = "+str(actual_total)+" in total")
    time_count = -np.inf
    time_start = time.time()

    actual_totals = dict(zip(x.iterkeys(), [actual_total]*len(x)))
    
    for k in xrange(len(iter)):

        i = iter[k]
        
        time_count = time_msg(time_count, k, iter, time_start)        

        # Restore the i'th cache from trace
        all_chain_remember(M,i)
        
        fs = {}
        for outercoll in x.iterkeys():
        
            # Add the covariate values on the prediction mesh to the appropriate caches.
            covariate_covariances = []
            for d in M.deterministics:
                if isinstance(d.value, pm.gp.Covariance):
                    if isinstance(d.value.eval_fun,CovarianceWithCovariates):
                        for innercoll, x_ in x[outercoll].iteritems():
                            d.value.eval_fun.add_values_to_cache(x_, pred_covariate_dict[outercoll][innercoll])

            for s in gp_submods:
                # Copy out the realization to avoid dependencies between outer collections.
                f_ = np.asscalar(copy.copy(s.f.value))
                fs[s] = lambda x, f=f_, V=pm.utils.value(nuggets[s]): f(x) + np.sqrt(V)*np.random.normal(size=x.shape[0])


            try:
                for j in xrange(n_per):

                    # Evaluate fields, and store them in argument dict for postproc
                    for postproc in postprocs:
                        g_vals = {}
                        for innercoll in x[outercoll].iterkeys():
                            g_ = g[postproc][outercoll][innercoll]
                            g_kwds = {'x':x[outercoll][innercoll]}
                            for s in gp_submods:
                                g_kwds[s.name]=fs[s]
                        
                            # Pull any extra variables needed for postprocessing out of trace
                    
                            for extra_arg in extra_g_args[g_]:
                                g_kwds[extra_arg] = pm.utils.value(getattr(M, extra_arg))

                            try:
                                g_vals[innercoll] = np.mean(g_(**g_kwds), axis=0)
                            except np.linalg.LinAlgError:
                                if continue_past_npd:
                                    warnings.warn('The observed covariance was not positive definite at the prediction locations.')
                                    raise np.linalg.LinAlgError
                                else:
                                    raise ValueError, 'The observed covariance was not positive definite at the prediction locations.'
                        
                        
                        h_kwds = copy.copy(g_vals)
                        for extra_arg in extra_h_args[outercoll][postproc]:
                            h_kwds[extra_arg] = pm.utils.value(getattr(M, extra_arg))
                        
                        h_val = h[postproc][outercoll](**h_kwds)

                        # Reduce surface into products needed
                        for f in fns:
                            products[outercoll][postproc][f] = f(products[outercoll][postproc][f], h_val, postproc.__name__)
            # apply_postprocs_and_reduce(M, n_per, M_preds, S_preds, postprocs, fns, products, postproc_args, extra_postproc_args, joint)    
            except np.linalg.LinAlgError:
                # This iteration is non-positive definite, _and_ the user has asked to continue past such iterations.
                actual_totals[outercoll] -= n_per
                continue

    if finalize is not None:
        for outercoll in x.iterkeys():
            products[outercoll] = dict(zip(postprocs, [finalize(products[outercoll][postproc], actual_total) for postproc in postprocs]))

    return products
        

def hdf5_to_samps(M, x, nuggets, burn, thin, total, fns, postprocs, pred_covariate_dict, finalize=None, continue_past_npd=False, joint=False):
    """
    Parameters:
        M : MCMC object
            Its database should have samples in it.
        x : array
            The lon, lat locations at which predictions are desired.
        nuggets : dict
            Should map the GP submodels of M to the corresponding nugget values,
            if any.
        burn : int
            Burnin iterations to discard.
        thin : int
            Number of iterations between ones that get used in the predictions.
        total : int
            Total number of iterations to use in thinning.
        fns : list of functions
            Each function should take four arguments: sofar, next, cols and i.
            Sofar may be None.
            The functions will be applied according to the reduce pattern.
        postprocs : list of functions
            These functions are applied to the realization before it is passed 
            to the fns.
        pred_covariate_dict : dict
            Maps covariate keys to values on x.
        finalize : function (optional)
            This function is applied to the products before returning. It should
            take a second argument which is the actual number of realizations
            produced.
    """    
    hf=M.db._h5file
    gp_submods = list(set(filter(lambda c: isinstance(c,pm.gp.GPSubmodel), M.containers)))
    f_labels = [gps.name for gps in gp_submods]
    
    products, postproc_args, extra_postproc_args = get_args(postprocs, fns, f_labels, M)
        
    iter = np.arange(burn,all_chain_len(hf),thin)

    M_preds = {}
    S_preds = {}

    if len(iter)==0:
        raise ValueError, 'You asked for %i burnin iterations with thinnnig %i but the chains are only %i iterations long.'%(burn, thin, all_chain_len(hf))
    if (total>len(iter)):
        n_per = total/len(iter)+1
        
    if (total<=len(iter)):
        n_per=1
        newthin=(all_chain_len(hf)-burn)/total
        iter = np.arange(burn,all_chain_len(hf),newthin)
    
    if len(iter)==0:
        raise ValueError, 'Check --total --thin and burn parameters, currently asking for zero realisations'        
    actual_total = n_per * len(iter)
    print("total chain length = "+str(all_chain_len(hf))+"\nburn = "+str(burn)+"\nthin = "+str(thin)+"\ntotal = "+str(total))
    print("will do "+str(len(iter))+" full iterations with "+str(n_per)+" nuggets each = "+str(actual_total)+" in total")
    time_count = -np.inf
    time_start = time.time()
    
    for k in xrange(len(iter)):
        
        i = iter[k]
        # Restore the i'th cache fram trace
        all_chain_remember(M,i)
        
        # Add the covariate values on the prediction mesh to the appropriate caches.
        covariate_covariances = []
        for d in M.deterministics:
            if isinstance(d.value, pm.gp.Covariance):
                if isinstance(d.value.eval_fun,CovarianceWithCovariates):
                    d.value.eval_fun.add_values_to_cache(x,pred_covariate_dict)
        
        time_count=time_msg(time_count, k, iter, time_start)
        
        try:
            for s in gp_submods:
                if joint:
                    M_preds[s] = pm.utils.value(s.M_obs)(x)
                    C_pred = pm.utils.value(s.C_obs)(x,x)
                    C_pred += pm.utils.value(nuggets[s])*np.eye(x.shape[0])
                    try:
                        S_preds[s] = np.linalg.cholesky(C_pred)
                    except np.linalg.LinAlgError:
                        if continue_past_npd:
                            warnings.warn('The observed covariance was not positive definite at the prediction locations.')
                            raise np.linalg.LinAlgError
                        else:
                            raise ValueError, 'The observed covariance was not positive definite at the prediction locations.'
                else:
                    # FIXME: long time in a single thread here. L168 of FullRankCovariance.py
                    M_preds[s], V_pred = pm.gp.point_eval(pm.utils.value(s.M_obs), pm.utils.value(s.C_obs), x)
                    if np.any(V_pred<0):
                        xbad = x[np.where(V_pred<0)][0]
                        xaug = np.vstack((s.mesh, xbad))
                        try:
                            np.linalg.cholesky(s.C.value(xaug,xaug))
                        except RuntimeError:
                            raise ValueError, 'Some elements of V_pred were negative. This problem cannot be attributed to non-positive definiteness.'

                        except np.linalg.LinAlgError:
                            if continue_past_npd:
                                warnings.warn('Some elements of V_pred were negative due to non-positive definiteness.')
                                raise np.linalg.LinAlgError
                            else:
                                raise ValueError, 'Some elements of V_pred were negative due to non-positive definiteness.'                
                    S_preds[s] = np.sqrt(V_pred + pm.utils.value(nuggets[s]))
                
        except np.linalg.LinAlgError:
            # This iteration is non-positive definite, _and_ the user has asked to continue past such iterations.
            actual_total -= n_per
            continue
        
        apply_postprocs_and_reduce(M, n_per, M_preds, S_preds, postprocs, fns, products, postproc_args, extra_postproc_args, joint)    

    if finalize is not None:
        return dict(zip(postprocs, [finalize(products[postproc], actual_total) for postproc in postprocs]))
    else:
        return products

def normalize_for_mapcoords(arr, max):
    "Used to create inputs to ndimage.map_coordinates."
    arr /= arr.max()
    arr *= max
    
def vec_to_raster(vec, fname, raster_path, out_name, unmasked, path='.'):
    """
    Converts a vector of outputs on a thin, unmasked, ravelled subset of an
    ascii grid to an ascii file matching the original grid.
    """
    
    # FXIME: Just resample in Fortran. Only draw from pixels that are inside the mask.
    # FIXME: Just do it bilinear, nothing fancy.
    
    # FIXME: Make this work with arbitrary mask types.
    
    if np.any(np.isnan(vec)):
        #raise ValueError, 'NaN in vec'
        print ("warning!! "+str(np.sum(np.isnan(vec)))+" of "+str()+" NaN values in vec - check output: "+out_name)
    
    lon,lat,data,type = import_raster(fname, os.path.join('..',raster_path))
    data = grid_convert(data,'y-x+','x+y+')
    data_thin = np.zeros(unmasked.shape)
    data_thin[unmasked] = vec
    
    mapgrid = np.array(mgrid[0:data.shape[0],0:data.shape[1]], dtype=float)
    normalize_for_mapcoords(mapgrid[0], data_thin.shape[0]-1)
    normalize_for_mapcoords(mapgrid[1], data_thin.shape[1]-1)
    
    if data_thin.shape == data.shape:
        out = np.ma.masked_array(data_thin, mask=data.mask)
    else:
        out = np.ma.masked_array(ndimage.map_coordinates(data_thin, mapgrid), mask=data.mask)
        
    if np.any(np.isnan(out)):
        warnings.warn('NaN in output')
    
    out_conv = grid_convert(out,'x+y+','y-x+')
    
    export_raster(lon,lat,out_conv,out_name,path,type)
    
    return lon,lat,out_conv
