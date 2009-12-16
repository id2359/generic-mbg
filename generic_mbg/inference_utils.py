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

def creeate_model(mod,db,input=None):
    """
    Takes either:
    - A module and a database object, or:
    - A module, a filename and a record array
    and returns an MCMC object, with step methods assigned.
    """
    
    if isinstance(db,pm.database.hdf5.Database):
        input = db._h5file.root.input_csv[:]
        
    if isinstance(input, np.ma.mrecords.MaskedRecords):
        msg = 'Error, could not parse input at following rows:\n'
        for name in input.dtype.names:
            if np.sum(input[name].mask)>0:
                msg += '\t%s: %s\n'%(name, str(np.where(input[name].mask)[0]+1))
        raise ValueError, msg

    lon = maybe_convert(input, 'lon', 'float')
    lat = maybe_convert(input, 'lat', 'float')
    mod_inputs = (lon,lat)
    if hasattr(input, 't'):
        t = maybe_convert(input, 't', 'float')
        x = combine_st_inputs(lon,lat,t)
        mod_inputs = mod_inputs + (t,)
    else:
        x = combine_spatial_inputs(lon,lat)

    non_cov_columns = {'cpus': o.ncpus}
    if hasattr(mod, 'non_cov_columns'):
        non_cov_coltypes = mod.non_cov_columns
    else:
        non_cov_coltypes = {}
    non_cov_colnames = non_cov_coltypes.keys()

    covariate_dict = {}
    for n in input.dtype.names:
        if n not in ['lon','lat','t']:
            if n in non_cov_colnames:
                non_cov_columns[n] = maybe_convert(input, n, non_cov_coltypes[n])
            else:
                covariate_dict[n]=maybe_convert(input, n, 'float')

    mod_inputs = mod_inputs + (covariate_dict,)

    # Create MCMC object, add metadata, and assign appropriate step method.

    if prev_db is None:
        M = pm.MCMC(make_model(*mod_inputs,**non_cov_columns),db='hdf5',dbname=hfname,complevel=1,complib='zlib')
        add_standard_metadata(M, mod.x_labels, *metadata_keys)
        M.db._h5file.createTable('/','input_csv',csv2rec(file(o.input_name,'U')))
        M.db._h5file.root.input_csv.attrs.shellargs = ' '.join(sys.argv[1:])
        M.db._h5file.root.input_csv.attrs.mod_name = mod_name
        M.db._h5file.root.input_csv.attrs.mod_commit = mod_commit
        M.db._h5file.root.input_csv.attrs.generic_commit = generic_commit
        M.db._h5file.root.input_csv.attrs.starttime = datetime.datetime.now()
        M.db._h5file.root.input_csv.attrs.input_filename = o.input_name

    else:
        M = pm.MCMC(make_model(*mod_inputs,**non_cov_columns),db=prev_db)
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

def chains(hf):
    return [gr for gr in hf.listNodes("/") if gr._v_name[:5]=='chain']

def all_chain_len(hf):
    return np.sum([len(chain.PyMCsamples) for chain in chains(hf)])
    
def all_chain_trace(hf, name):
    return np.concatenate([np.ravel(chain.PyMCsamples.col(name)) for chain in chains(hf)])
    
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
            
def all_chain_remember(M, name, i):
    raise NotImplementedError, 'May need to be fixed in PyMC.'
    
def add_standard_metadata(M, x_labels, *others):
    """
    Adds the standard metadata to an hdf5 archive.
    """
    
    hf = M.db._h5file
    hf.createGroup('/','metadata')
        
    hf.createVLArray(hf.root.metadata, 'covariates', tb.ObjectAtom())
    hf.root.metadata.covariates.append(M.covariate_dicts)
    
    for label in set(x_labels.itervalues()):
        hf.createArray(hf.root.metadata, label, getattr(M, label))
        
    for label in set(others):
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
        self.values = [value]
    def add_value_to_cache(mesh, value):
        self.meshes.append(mesh)
        self.values.append(value)
    def __call__(self, mesh):
        for m,i in enumerate(self.meshes):
            if np.all(m==mesh):
                return self.values[i]
        raise RuntimeError, 'The given mesh is not in the cache.'
        
def CovarianceWithCovariates(object):
    """
    A callable that adds some covariates to the given covariance function C.
    
    The output is:
        ~C(x,y) = C(x,y) + fac(m + \sum_{n\in names} outer(~cov[n](x), ~cov[n](y)))

    m is the number of covariates.
        
    ~cov[n](x) is (cov[n](x)-mu[n])/sig[n], where mu[n] and sig[n] are the mean and
    standard deviation of the values passed into the init method.
    """
    def __init__(self, cov_fun, mesh, cv, fac=1e6, diag_safe=False):
        self.cv = cv
        self.m = len(cv)
        self.means = dict([(k,np.mean(v)) for k,v in cv.iteritems()])
        self.stds = dict([(k,np.std(v)) for k,v in cv.iteritems()])
        self.evaluators = dict([(k,CachingCovariateEvaluator(mesh, v)) for k,v in cv.iteritems()])
        self.cov_fun = cov_fun
        self.fac = fac
        self.diag_safe = diag_safe

    def eval_covariates(self, x):
        return dict([(k,(self.evaluators[k](x)-self.means[k])/self.stds[k]) for k in self.cv.iterkeys()])
        
    def add_values_to_cache(mesh, new_cv):
        for k,v in self.evaluators.iteritems():
            v.add_value_to_cache(new_cv[k])

    def __call__(self, x, y=None, *args, **kwds):
        # FIXME: Use prediction_utils.square_and_sum, and crossmul_and_sum here.
        x_evals = self.eval_covariates(x)
        
        # Evaluate with one argument:
        if y None:
            if self.diag_safe:
                Cbase = self.cov_fun.params['amp']**2 * np.ones(x.shape[0])
            else:
                Cbase = self.cov_fun(x,y,*args,**kwds)
            for i in xrange(len(Cbase)):
                C = Cbase + self.fac * (self.m + np.sum([x_evals[k]**2 for k in self.cv.iterkeys()], axis=0))
                return C
        
        # Evaluate with both arguments:
        else:
            Cbase = self.cov_fun(x,y,*args,**kwds)
            if x is y:
                y_evals = x_evals
            else:
                y_evals = self.eval_covariates(y)
            C = Cbase + self.fac * (self.m + np.sum([np.outer(x_evals[k], y_evals[k]) for k in self.cv.iterkeys()], axis=0))
            return C

def sample_covariates(covariate_dict, C_eval, d):
    """
    Samples covariates back in when they have been marginalized away.
        - covariate_dict : {name : value-on-input, prior-variance}
        - M_eval : array. Probably zeros, unless you did something fancy in the mean.
        - C_eval : covariance of d | covariates, m
        - d : current deviation from mean of covariates' immediate child.
    """
    raise NotImplementedError, 'This has not been ported to PyMC 2.1 yet.'
    
    # Extract keys to list to preserve order.
    n = covariate_dict.keys()
    
    cvv = [covariate_dict[k] for k in n]
    x = np.asarray([v[0] for v in cvv])
    prior_var = np.diag([v[1] for v in cvv])
    
    prior_offdiag = np.dot(prior_var,x).T
    prior_S = np.linalg.cholesky(np.asarray(C_eval) + np.dot(prior_offdiag, x))
    pm.gp.trisolve(prior_S, prior_offdiag, uplo='L', transa='N', inplace=True)
    post_C = prior_var - np.dot(prior_offdiag.T, prior_offdiag)
    post_mean = np.dot(prior_offdiag.T, pm.gp.trisolve(prior_S, d, uplo='L', transa='N'))
    
    new_val = pm.rmv_normal_cov(post_mean, post_C).squeeze()

    return dict(zip(n, new_val))

def get_d_C_eval(hf, f_label, nugget_label, i, mesh):
    """Utility fn"""
    if type(f_label) == type('str'):
        d = all_chain_getitem(hf, f_label, i, vl=False)
    else:
        d = f_label

    C = all_chain_getitem(hf, 'C', i, vl=True)
    if nugget_label is not None:
        nug = all_chain_getitem(hf, nugget_label, i, vl=False)

    C_eval = C(mesh, mesh) + nug*np.eye(np.sum(mesh.shape[:-1]))
    return d, C_eval

def covariate_trace(hf, f_label, nugget_label=None, burn=0, thin=1):
    """
    Produces a covariate trace from an existing hdf5 chain.
        - chain : hdf5 group
        - meta : hdf5 group
        - f_label : string or array
        - nugget_label : string
    """
    meta = hf.root.metadata
    
    covariate_dict = meta.covariates[0]

    out = dict.fromkeys(covariate_dict)
    for k in out.keys():
        out[k] = []
        
    if nugget_label is None:
        mesh = meta.logp_mesh[:]
    else:
        mesh = meta.data_mesh[:]

    n = all_chain_len(hf)
    time_count = -np.inf
    time_start = time.time()
        
    for i in xrange(burn,n,thin):

        if time.time() - time_count > 10:
            print ((i*100)/n), '% complete',
            time_count = time.time()     
            if i > 0:       
                print 'expect results '+time.ctime((time_count-time_start)*n/float(i)+time_start)        
            else:
                print

        d, C_eval = get_d_C_eval(hf, f_label, nugget_label, i, mesh)
        cur_vals = sample_covariates(covariate_dict, C_eval, d)

        for k, v in cur_vals.iteritems():
            out[k].append(v)

    return dict([(k,np.array(v)) for k,v in out.iteritems()])