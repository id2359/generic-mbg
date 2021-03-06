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


from __future__ import division
import pymc as pm
import pylab as pl
import tables as tb
import numpy as np
from scipy import integrate
from histogram_utils import *
import time
import os

__all__ = ['validation_fns']

def roc(p_samps, n_samps, pos, neg):
    """
    Plots the receiver-operator characteristic.
    """
    t = np.linspace(0,1,500)
    tp = [1]
    fp = [1]
    tot_pos = np.float(np.sum(pos))
    tot_neg = np.float(np.sum(neg))
    
    marginal_p = np.mean(p_samps,axis=0)
    
    for i in xrange(len(t)):
        where_yes = np.where(marginal_p > t[i])
        where_no = np.where(marginal_p < t[i])
        where_eq = np.where(marginal_p == t[i])
        fp_here = (np.sum(neg[where_yes]) + np.sum(.5*neg[where_eq]))/tot_neg
        if fp_here < 1:
            tp.append((np.sum(pos[where_yes]) + np.sum(.5*pos[where_eq]))/tot_pos)
            fp.append(fp_here)
        if fp_here == 0:
            break
            
    fp = np.array(fp)
    tp = np.array(tp)
        
    pl.fill(np.concatenate((fp,[0,1,1])), np.concatenate((tp,[0,0,1])), facecolor='.8', edgecolor='k', linewidth=1)
    pl.plot([0,1],[0,1],'k-.')
    
    pl.xlabel('False positive rate')
    pl.ylabel('True positive rate')
    
    AUC = -np.sum(np.diff(fp)*(tp[1:] + tp[:-1]))/2.
    if np.isnan(AUC):
        raise ValueError, 'AUC is NaN.'
    pl.title('AUC: %.3f'%AUC)
    pl.axis([0,1,0,1])
    
    return np.rec.fromarrays((fp,tp),names='false-pos,true-pos')
    
def scatter(p_samps, n_samps, pos, neg):
    """
    Plots the expected fraction positive against the observed 
    fraction positive.
    """
    p_pred = np.mean(p_samps, axis=0)
    p_obs = pos/(pos+neg).astype('float')
    
    pl.plot(p_obs, p_pred,'r.', markersize=2)
    urc = max(p_obs.max(),p_pred.max())*1.1
    
    pl.plot([0,urc],[0,urc],'k-.')
    pl.xlabel('Observed frequency')
    pl.ylabel('Expected freuency')
    pl.axis([0,urc,0,urc])
    
    return np.rec.fromarrays((p_pred,p_obs),names='predicted-pos-frac,observed-pos-frac')
    
def coverage(p_samps, n_samps, pos, neg):
    """
    Plots the coverage plot in the lower right panel of mbgw.
    """
    obs_quantiles = np.array([np.sum(n_samps[:,i] > pos[i])+.5*np.sum(n_samps[:,i] == pos[i]) for i in xrange(len(pos))], dtype='float')/n_samps.shape[0]
    pt = np.linspace(0,1,500)
    cover = np.array([np.sum(obs_quantiles<pti) for pti in pt], dtype='float')/len(obs_quantiles)
    pl.plot([0,1],[0,1],'k-.')
    pl.plot(pt,cover,'k-')
    pl.xlabel('Predictive quantile')
    pl.ylabel('Fraction of observations below quantile')
    pl.axis([0,1,0,1])
    
    return np.rec.fromarrays((pt,cover),names='probability,coverage')
            
validation_fns = [roc,scatter,coverage]