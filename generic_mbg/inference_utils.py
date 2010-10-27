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
import time
import tables as tb
from st_cov_fun import my_st
from histogram_utils import iinvlogit, isinvlogit, iamul, iasq, icsum, subset_eq, iasadd, meshmatch
from pylab import csv2rec,rec2csv
import inspect
import copy
import os, sys, imp

def reload_model(mod, hf):
    mod_path, mod_name = os.path.split(mod)
    mod_basename, mod_ext = os.path.splitext(mod_name)
    mod_search_path = [mod_path, os.getcwd()] + sys.path
    mod = imp.load_module(mod_basename, *imp.find_module(mod_basename, mod_search_path))

    return create_model(mod,pm.database.hdf5.load(hf))

def close(f, **kwds):
    "For god's sake. Major symbol capture possibility here."
    fargs, fvarargs, fvarkw, fdefault=inspect.getargspec(f)
    
    if fvarargs is not None:
        raise ValueError, 'Cannot close a function with varargs'
    
    internal_f_name = 'f'
    while internal_f_name in fargs:
        internal_f_name = internal_f_name + '_'
    
    outer_strs = []
    default_strs = ['%s=f'%internal_f_name]
    inner_strs = []
    
    for farg_index, farg_d in enumerate(fargs):
        farg_index_=farg_index-(len(fargs)-len(fdefault or []))
        if farg_d in kwds.keys():
            exec('%s=kwds[farg_d]'%farg_d)
            default_strs.append('%s=%s'%(farg_d,farg_d))
        else:
            if farg_index_>=0:
                exec('%s=fdefault[farg_index_]'%farg_d)
                outer_strs.append('%s=%s'%(farg_d,farg_d))
            else:
                outer_strs.append(farg_d)
    outer_strs = outer_strs + default_strs

    inner_strs = copy.copy(fargs)
    
    if fvarkw is not None:
        outer_strs.append('**%s'%fvarkw)
        inner_strs.append('**%s'%fvarkw)
    
    exec("""def f_(%s):
    return %s(%s)"""%(','.join(outer_strs),internal_f_name,','.join(inner_strs)))

    f_.__name__ = f.__name__

    return f_

def maybe_convert(ra, field, dtype):
    """
    Tries to cast given field of given record array to given dtype. 
    Raises helpful error on failure.
    """
    arr = ra[field]
    try:
        return arr.astype(dtype)
    except:
        for i in xrange(len(arr)):
            try:
                np.array(arr[i],dtype=dtype)
            except:
                raise ValueError, 'Input column %s, element %i (starting from zero) is %s,\n which cannot be cast to %s'%(field,i,arr[i],dtype)    

def invlogit(x):
    """A shape-preserving, in-place, threaded inverse logit function."""
    if np.prod(np.shape(x))<10000:
        return pm.flib.invlogit(x)
    if not x.flags['F_CONTIGUOUS']:
        raise ValueError, 'x is not Fortran-contiguous'
    cmin, cmax = pm.thread_partition_array(x)        
    pm.map_noreturn(iinvlogit, [(x,cmin[i],cmax[i]) for i in xrange(len(cmax))])
    return x
    
def stukel_invlogit(x,a1,a2):
    """A shape-preserving, in-place, threaded inverse Stukel's logit function."""
    if np.prod(np.shape(x))<10000:
        return pm.flib.stukel_invlogit(x,a1,a2)
    if not x.flags['F_CONTIGUOUS']:
        raise ValueError, 'x is not Fortran-contiguous'
    cmin, cmax = pm.thread_partition_array(x)        
    pm.map_noreturn(isinvlogit, [(x,a1,a2,cmin[i],cmax[i]) for i in xrange(len(cmax))])
    return x

def fast_inplace_mul(a,s):
    """Multiplies a by s in-place and returns a."""
    a = np.atleast_2d(a)
    s = np.atleast_2d(s)
    cmin, cmax = pm.thread_partition_array(a)
    pm.map_noreturn(iamul, [(a,s,cmin[i],cmax[i]) for i in xrange(len(cmax))])
    return a
    
def fast_inplace_scalar_add(a,s):
    """Adds s to a in-place and returns a. s should be a scalar."""
    a = np.atleast_2d(a)
    cmin, cmax = pm.thread_partition_array(a)
    pm.map_noreturn(iasadd, [(a,s,cmin[i],cmax[i]) for i in xrange(len(cmax))])
    return a

def fast_inplace_square(a):
    """Squares a in-place and returns it."""
    cmin, cmax = pm.thread_partition_array(a)
    pm.map_noreturn(iasq, [(a,cmin[i],cmax[i]) for i in xrange(len(cmax))])
    return a
    
def crossmul_and_sum(c,x,d,y):
    """Returns C + \sum_i d_i*outer(x[i,:],y[i,:])"""
    cmin, cmax = pm.thread_partition_array(y)
    pm.map_noreturn(icsum, [(c,x,d,y,cmin[i],cmax[i]) for i in xrange(len(cmax))])
    return c

class zero_inflate_logp(object):
    def __init__(self, logp_fn):
        self.logp_fn = logp_fn
    def __call__(self, x, p0, *args, **kwds):
        n_zero=np.sum(x==0)
        return n_zero*np.log(p0) + (np.alen(x)-n_zero)

def create_model(mod,db,input=None):
    """
    Takes either:
    - A module and a database object, or:
    - A module, a filename and a record array
    and returns an MCMC object, with step methods assigned.
    """
    
    if isinstance(db,pm.database.hdf5.Database):
        if input is not None:
            raise ValueError, 'Input provided with preexisting db.'
        input = db._h5file.root.input_csv[:]
        prev_db = db
        hfname = db._h5file.filename
    else:
        if input is None:
            raise ValueError, 'No input or preexisting db provided.'
        prev_db=None
        hfname = db
    
    rec2csv(input,'%s-input-data.csv'%hfname)
        
    lon = maybe_convert(input, 'lon', 'float')
    lat = maybe_convert(input, 'lat', 'float')
    mod_inputs = (lon,lat)
    if 't' in input.dtype.names:
        t = maybe_convert(input, 't', 'float')
        x = combine_st_inputs(lon,lat,t)
        mod_inputs = mod_inputs + (t,)
    else:
        x = combine_spatial_inputs(lon,lat)
    
    non_cov_columns = {}
    if hasattr(mod, 'non_cov_columns'):
        non_cov_coltypes = mod.non_cov_columns
    else:
        non_cov_coltypes = {}
    non_cov_colnames = non_cov_coltypes.keys()

    covariate_keys = []
    for n in input.dtype.names:
        if n not in ['lon','lat','t']:
            if n in non_cov_colnames:
                non_cov_columns[n] = maybe_convert(input, n, non_cov_coltypes[n])
            else:
                covariate_keys.append(n)

    mod_inputs = mod_inputs + ('%s-input-data.csv'%hfname,covariate_keys,)

    # Create MCMC object, add metadata, and assign appropriate step method.

    if prev_db is None:
        M = pm.MCMC(mod.make_model(*mod_inputs,**non_cov_columns),db='hdf5',dbname=hfname,dbcomplevel=1,dbcomplib='zlib')
    else:
        M = pm.MCMC(mod.make_model(*mod_inputs,**non_cov_columns),db=prev_db)
        M.restore_sampler_state()
        
    # Pass MCMC object through the module's mcmc_init function.
    mod.mcmc_init(M)
    
    # Restore step method state if possible.
    M.assign_step_methods() 
    if prev_db is not None:
        M.restore_sm_state()

    return M

def spatial_mean(x, m_const):
    return m_const*np.ones(x.shape[0])
    
def zero_mean(x):
    return np.zeros(x.shape[:-1])

def st_mean_comp(x, m_const, t_coef):
    lon = x[:,0]
    lat = x[:,1]
    t = x[:,2]
    return m_const + t_coef * t
    
def uniquify_tol(disttol, ttol, *cols):
    locs = [np.array([col[0] for col in cols])]
    fi = [0]
    ui = [0]
    dx = np.empty(1)
    for i in xrange(1,len(cols[0])):

        # If repeat location, add observation
        loc = np.array([col[i] for col in cols])
        for j in xrange(len(locs)):
            pm.gp.geo_rad(dx, np.atleast_2d(loc[:2]*np.pi/180.), np.atleast_2d(locs[j][:2]*np.pi/180.))
            if len(cols)>2:
                dt = np.abs(loc[2]-locs[j][2])
            else:
                dt = 0
            if dx[0]<=disttol and dt<=ttol:
                fi.append(j)
                break

        # Otherwise, new obs
        else:
            locs.append(loc)
            fi.append(max(fi)+1)
            ui.append(i)
    fi = np.array(fi)
    ti = [np.where(fi == i)[0] for i in xrange(max(fi)+1)]
    ui = np.asarray(ui)

    locs = np.array(locs)
    if len(cols)==3:
        data_mesh = combine_st_inputs(*cols)
        logp_mesh = combine_st_inputs(locs[:,0], locs[:,1], locs[:,2])
    else:
        data_mesh = combine_spatial_inputs(*cols)
        logp_mesh = combine_spatial_inputs(locs[:,0], locs[:,1])

    return data_mesh, logp_mesh, fi, ui, ti
    
def uniquify(*cols):

    locs = [tuple([col[0] for col in cols])]
    fi = [0]
    ui = [0]
    for i in xrange(1,len(cols[0])):

        # If repeat location, add observation
        loc = tuple([col[i] for col in cols])
        if loc in locs:
            fi.append(locs.index(loc))

        # Otherwise, new obs
        else:
            locs.append(loc)
            fi.append(max(fi)+1)
            ui.append(i)
    fi = np.array(fi)
    ti = [np.where(fi == i)[0] for i in xrange(max(fi)+1)]
    ui = np.asarray(ui)

    locs = np.array(locs)
    if len(cols)==3:
        data_mesh = combine_st_inputs(*cols)
        logp_mesh = combine_st_inputs(locs[:,0], locs[:,1], locs[:,2])
    else:
        data_mesh = combine_spatial_inputs(*cols)
        logp_mesh = combine_spatial_inputs(locs[:,0], locs[:,1])

    return data_mesh, logp_mesh, fi, ui, ti

def combine_spatial_inputs(lon,lat):
    # Convert latitude and longitude from degrees to radians.
    lon = lon*np.pi/180.
    lat = lat*np.pi/180.
    
    # Make lon, lat tuples.
    data_mesh = np.vstack((lon, lat)).T 
    return data_mesh
    
def combine_st_inputs(lon,lat,t):
    # Convert latitude and longitude from degrees to radians.
    lon = lon*np.pi/180.
    lat = lat*np.pi/180.

    # Make lon, lat, t triples.
    data_mesh = np.vstack((lon, lat, t)).T 
    return data_mesh

def add_standard_metadata(M, *labels):
    """
    Adds the standard metadata to an hdf5 archive.
    """
    
    hf = M.db._h5file
    hf.createGroup('/','metadata')
        
    for label in set(labels):
        vla=hf.createVLArray(hf.root.metadata, label, tb.ObjectAtom())
        vla.append(getattr(M,label))
        
class CachingCovariateEvaluator(object):
    """
    Evaluate this object on an array. If it has already been
    told what its value on the array is, it will return that value.
    Otherwise, it will throw an error.
    """

    def __init__(self, mesh, value):
        self.meshes = [mesh]
        shift = (value.max()+value.min())/2.
        scale = (value.max()-value.min())/2.
        self.values = [((value-shift)/scale).astype('float')]
        if np.any(np.isnan(value)):
            raise ValueError, 'NaN in covariate values'
        self.shift = shift
        self.scale = scale

    def add_value_to_cache(self, mesh, value):
        if np.any(np.isnan(value)):
            raise ValueError, 'NaN in covariate values'
        # elif len(set(value))==1:
        #     raise ValueError, 'Only one value in covariate. Not going to learn much here.'
        self.meshes.append(mesh)
        self.values.append(((value-self.shift)/self.scale).astype('float'))

    def __call__(self, mesh):
        # FIXME: This should be able to piece contiguous blocks together from the different meshes.
        for i,m in enumerate(self.meshes):
            start,stop = subset_eq(m,mesh)
            if start>-1 and stop>-1:
                if stop-start != mesh.shape[0]:
                    raise ValueError
                return self.values[i][start:stop]

        # raise RuntimeError, 'The given mesh is not present as a contiguous block in cache.'
        # print( "The given mesh is not present as contiguous block in cache, checking if present in non-contiguous blocks")

        tempvals = np.empty(mesh.shape[0])
        tempvals.fill(np.nan)
        for m,v in zip(self.meshes, self.values):
            meshmatch(tempvals,mesh,m,v)

        # # initialise vector for extracted values
        # tempvals = np.repeat(np.nan,len(mesh[:,0]))
        #     
        # # loop through elements of new mesh and attempt to find them in cached mesh ii
        # for jj in xrange(0,len(mesh[:,0])):
        # 
        #     print("On element "+str(jj)+" of "+str(len(mesh[:,0])))
        # 
        #     # loop through different meshes stored in cache
        #     for ii,m in enumerate(self.meshes):
        # 
        #         matchid=((m==mesh[jj,:]).sum(axis=1)==2)
        # 
        #         # if we have a match in cached mesh ii..
        #         if(sum(matchid)>0):
        # 
        #             # extract value for this mesh location from cache
        #             tempvals[jj] = self.values[ii][np.where(matchid)[0][0]]
        #             break

        # check all mesh locations have been identified in cache
        notfound = np.sum(np.isnan(tempvals)) 
            
        if(notfound>0):
            raise RuntimeError, str(notfound)+' of '+str(len(mesh[:,0]))+' elements of given mesh not present in the cache'           

        return tempvals

        
class CovarianceWithCovariates(object):
    """
    A callable that adds some covariates to the given covariance function C.
    
    The output is:
        ~C(x,y) = C(x,y) + fac(m + \sum_{n\in names} outer(~cov[n](x), ~cov[n](y)))

    m is the number of covariates.
        
    ~cov[n](x) is (cov[n](x)-mu[n])/sig[n], where mu[n] and sig[n] are the mean and
    standard deviation of the values passed into the init method.
    """
    def __getstate__(self):
        return (self.cov_fun, self.file, self.keys, self.ui, self.fac, self.ampsq_is_diag)
    def __setstate__(self, state):
        self.__init__(*state)
                        
    def __init__(self, cov_fun, file, keys, ui, fac=1e6, ampsq_is_diag=False, ra=None):

        if ra is None:
            ra = csv2rec(file)
        mesh = np.vstack((ra.lon[ui], ra.lat[ui]))*np.pi/180.
        # if 't' in ra.dtype.names:
        #     mesh = np.vstack((mesh, ra.t[ui]))
        mesh = mesh.T
        self.m = len(keys)
        self.labels = keys
        self.file = file
        self.keys = keys
        self.ui = ui
        self.meshes = [mesh]
        self.dicts = [dict([(k,ra[k][ui]) for k in self.labels])]
        self.evaluators = dict([(k,CachingCovariateEvaluator(mesh[:,:2], ra[k][ui])) for k in self.labels])
        self.cov_fun = cov_fun
        self.fac = fac
        self.ampsq_is_diag = ampsq_is_diag
        if isinstance(fac, dict):
            self.mfac = fac['m']
            self.privar = np.array([fac[l] for l in self.labels])
        else:
            self.privar = np.ones(len(self.labels))*fac
            self.mfac = fac
        self.mfac *= (self.m+1)

    def diag_base_call(self, x, *args, **kwds):
        if hasattr(self.cov_fun, 'diag_call'):
            return self.cov_fun.diag_call(x,*args,**kwds)
        else:
            return self.cov_fun(x,y=None,*args,**kwds)

    def diag_covariate_call(self, x, Vbase=None):
        if Vbase is None:
            Vbase = np.zeros(x.shape[0])
        # Evaluate with one argument:
        x_evals = self.eval_covariates(x)        
        if len(self.labels)>0:
            return Vbase + np.sum(self.privar * x_evals.T**2, axis=1) + self.mfac
        else:
            return Vbase + self.mfac


    def diag_call(self, x, *args, **kwds):
        Vbase = self.diag_base_call(x,*args,**kwds)
        return self.diag_covariate_call(x, Vbase)
        
    def eval_covariates(self, x):
        out = np.asarray([self.evaluators[k](x[:,:2]) for k in self.labels], order='F')
        return out
        # return np.asarray([np.ones(len(x)) for k in self.labels], order='F')
        
    def add_values_to_cache(self, mesh, new_cv):
        self.meshes.append(mesh)
        self.dicts.append(new_cv)
        for k,v in self.evaluators.iteritems():
            v.add_value_to_cache(mesh[:,:2], new_cv[k])

    def base_call(self, x, y, *args, **kwds):
        return self.cov_fun(x,y,*args,**kwds)
        
    def covariate_call(self, x, y, Cbase=None):
        if Cbase is None:
            Cbase = np.zeros((x.shape[0],y.shape[0]))
        if len(self.evaluators) > 0:
            x_evals = self.eval_covariates(x)
            if x is y:
                y_evals = x_evals
            else:
                y_evals = self.eval_covariates(y)
            C = crossmul_and_sum(Cbase, x_evals, self.privar, y_evals)
            C = fast_inplace_scalar_add(C, self.mfac)
        else:
            C = fast_inplace_scalar_add(Cbase, self.mfac)
            
        return C


    def __call__(self, x, y=None, *args, **kwds):
        
        if y is None:
            return self.diag_call(x, *args, **kwds)
        
        # Evaluate with both arguments:
        Cbase = self.cov_fun(x,y,*args,**kwds)
        return self.covariate_call(x,y,Cbase)
        
def sample_covariate_values(s):
    """
    Draws a sample from the full conditional distribution of the covariate 
    coefficients integrated out into a CovarianceWithCovariates instance.
    """
    f = s.f_eval.value
    M = s.M_eval.value
    C = s.C.value
    C_eval = s.C_eval.value
    mesh = s.mesh
    
    if not isinstance(C.eval_fun, CovarianceWithCovariates):
        raise TypeError, 'Argument C must be a CovarianceWithCovariates instance.'
    delta = f-M
    privar = C.eval_fun.privar
    m = C.eval_fun.m
    vars = C.eval_fun.evaluators.keys()
    x = np.array([C.eval_fun.evaluators[k](mesh[:,:2]) for k in vars])
    xV = x.T*privar
    xVxT = np.dot(x.T,xV.T)
    
    delta_S = np.linalg.cholesky(np.asarray(C_eval))
    offdiag = pm.gp.trisolve(delta_S, xV, uplo='L', transa='N')
    delta_S_delta = pm.gp.trisolve(delta_S, delta, uplo='L', transa='N')
    
    post_M = np.dot(offdiag.T, delta_S_delta)
    post_C = np.diag(privar)-np.dot(offdiag.T,offdiag)
    val = pm.rmv_normal_cov(post_M, post_C)
    
    return dict(zip(vars, val))
