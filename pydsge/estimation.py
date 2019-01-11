#!/bin/python2
# -*- coding: utf-8 -*-
import numpy as np
import warnings
import os
import time
import emcee
from .stats import InvGamma, summary, mc_mean

def wrap_sampler(p0, nwalkers, ndim, ndraws, priors, ncores, update_freq, description, info):
    ## very very dirty hack 

    import tqdm
    import pathos

    ## globals are *evil*
    global lprob_global

    ## import the global function and hack it to pretend it is defined on the top level
    def lprob_local(par):
        return lprob_global(par)

    loc_pool    = pathos.pools.ProcessPool(ncores)

    sampler = emcee.EnsembleSampler(nwalkers, ndim, lprob_local, pool = loc_pool)

    if not info: np.warnings.filterwarnings('ignore')

    pbar    = tqdm.tqdm(total=ndraws, unit='sample(s)', dynamic_ncols=True)

    cnt     = 0
    for result in sampler.sample(p0, iterations=ndraws):
        if update_freq and pbar.n and not pbar.n % update_freq:
            pbar.write('')
            if description is not None:
                pbar.write('MCMC summary from last %s iterations (%s):' %(update_freq,str(description)))
            else:
                pbar.write('MCMC summary from last %s iterations:' %update_freq)
            pbar.write(str(summary(sampler.chain[:,pbar.n-update_freq:pbar.n,:], priors).round(3)))
        pbar.update(1)

    loc_pool.close()
    loc_pool.join()
    loc_pool.clear()

    if not info: np.warnings.filterwarnings('default')

    pbar.close()

    return sampler


def bayesian_estimation(self, N = None, P = None, R = None, ndraws = 500, tune = None, ncores = None, nwalkers = 100, maxfev = 2500, update_freq = None, info = False):

    import pathos
    import scipy.stats as ss
    import scipy.optimize as so
    import tqdm
    from .plots import traceplot, posteriorplot

    if ncores is None:
        ncores    = pathos.multiprocessing.cpu_count()

    if tune is None:
        tune    = int(ndraws*4/5.)

    if update_freq is None:
        update_freq     = int(ndraws/4.)

    description     = None

    if hasattr(self, 'description'):
        description     = self.description

    self.preprocess(info=info)

    ## dry run before the fun beginns
    self.create_filter(P = P, R = R, N = N)
    self.get_ll()
    print()
    print("Model operational. Ready for estimation.")
    print()

    par_fix     = np.array(self.par).copy()

    p_names     = [ p.name for p in self.parameters ]
    priors      = self['__data__']['estimation']['prior']
    prior_arg   = [ p_names.index(pp) for pp in priors.keys() ]

    ## add to class so that it can be stored later
    self.par_fix    = par_fix
    self.prior_arg  = prior_arg
    self.ndraws     = ndraws

    init_par    = par_fix[prior_arg]

    ndim        = len(priors.keys())

    print('Starting to add parameters to the prior distribution:')

    priors_lst     = []
    for pp in priors:
        dist    = priors[str(pp)]
        pmean = dist[1]
        pstdd = dist[2]

        ## simply make use of frozen distributions
        if str(dist[0]) == 'uniform':
            priors_lst.append( ss.uniform(loc=pmean, scale=pmean+pstdd) )
        elif str(dist[0]) == 'inv_gamma':
            priors_lst.append( InvGamma(a=pmean, b=pstdd) )
        elif str(dist[0]) == 'normal':
            priors_lst.append( ss.norm(loc=pmean, scale=pstdd) )
        elif str(dist[0]) == 'gamma':
            b = pstdd**2/pmean
            a = pmean/b
            priors_lst.append( ss.gamma(a, scale=b) )
        elif str(dist[0]) == 'beta':
            a = (1-pmean)*pmean**2/pstdd**2 - pmean
            b = a*(1/pmean - 1)
            priors_lst.append( ss.beta(a=1, b=1) )
        else:
            raise ValueError(' Distribution *not* implemented: ', str(dist[0]))
        print('     Adding parameter %s as %s with mean %s and std %s...' %(pp, dist[0], pmean, pstdd))


    def llike(parameters):

        if info == 2:
            st  = time.time()

        with warnings.catch_warnings(record=True):
            try: 
                warnings.filterwarnings('error')
                
                par_fix[prior_arg]  = parameters
                par_active_lst  = list(par_fix)

                self.get_sys(par_active_lst)
                self.preprocess(info=info)

                self.create_filter(P = P, R = R, N = N)

                self.enkf.P  *= 1e1

                ll  = self.get_ll()

                if info == 2:
                    print('Sample took '+str(np.round(time.time() - st))+'s.')

                return ll

            except:

                if info == 2:
                    print('Sample took '+str(np.round(time.time() - st))+'s. (failure)')

                return -np.inf

    def lprior(pars):
        prior = 0
        for i in range(len(priors_lst)):
            prior   += priors_lst[i].logpdf(pars[i])
        return prior

    def lprob(pars):
        return lprior(pars) + llike(pars)
    
    global lprob_global

    lprob_global    = lprob
    prior_names     = [ pp for pp in priors.keys() ]

    class func_wrap(object):
        ## thats a wrapper to have a progress par in the posterior maximization
        
        name = 'func_wrap'

        def __init__(self, init_par):

            self.n      = 0
            self.maxfev = maxfev
            self.pbar   = tqdm.tqdm(total=maxfev, dynamic_ncols=True)
            self.init_par   = init_par
            self.st     = 0
            self.update_ival    = 1
            self.timer  = 0
            self.res_max    = np.inf

        def __call__(self, pars):

            self.res   = -lprob(pars)
            self.x     = pars

            ## better ensure we're not just running with the wolfs when maxfev is hit
            if self.res < self.res_max:
                self.res_max    = self.res
                self.x_max      = self.x

            self.n      += 1
            self.timer  += 1

            if self.timer == self.update_ival:
                self.pbar.update(self.update_ival)

                ## prints information snapshots
                if update_freq and not self.n % update_freq:
                    ## getting the number of colums isn't that easy
                    with os.popen('stty size', 'r') as rows_cols:
                        cols            = rows_cols.read().split()[1]
                        self.pbar.write('Current best guess:')
                        ## split the info such that it is readable
                        lnum            = (len(priors)*8)//(int(cols)-8) + 1
                        priors_chunks   = np.array_split(np.array(prior_names), lnum)
                        vals_chunks     = np.array_split([round(m_val, 3) for m_val in self.x_max], lnum)
                        for pchunk, vchunk in zip(priors_chunks, vals_chunks):
                            row_format ="{:>8}" * (len(pchunk) + 1)
                            self.pbar.write(row_format.format("", *pchunk))
                            self.pbar.write(row_format.format("", *vchunk))
                            self.pbar.write('')
                    self.pbar.write('')

                difft   = time.time() - self.st
                if difft < 0.5:
                    self.update_ival *= 2
                if difft > 1 and self.update_ival > 1:
                    self.update_ival /= 2

                self.pbar.set_description('ll: '+str(-self.res.round(5)).rjust(12, ' ')+' ['+str(-self.res_max.round(5))+']')
                self.st  = time.time()
                self.timer  = 0

            if self.n >= maxfev:
                raise StopIteration

            return self.res

        def go(self):

            try:
                f_val       = -np.inf
                self.x      = self.init_par

                res         = so.minimize(self, self.x, method='Powell', tol=1e-3)

                self.pbar.close()
                print('')
                if self.res_max < res['fun']:
                    print(res['message'], 'Maximization returned value lower than actual (known) optimum ('+str(-self.res_max)+' > '+str(-self.res)+').')
                else:
                    print(res['message'], 'Log-likelihood is '+str(np.round(-res['fun'],5))+'.')
                print('')

            except (KeyboardInterrupt, StopIteration) as e:
                self.pbar.close()
                print('')
                print('Maximum number of function calls exceeded, exiting. Log-likelihood is '+str(np.round(-self.res_max,5))+'...')
                print('')

            return self.x_max

    if maxfev:

        print()
        if not info:
            np.warnings.filterwarnings('ignore')
            print('Maximizing posterior mode density (meanwhile warnings are disabled):')
        else:
            print('Maximizing posterior mode density:')
        print()

        result      = func_wrap(init_par).go()
        np.warnings.filterwarnings('default')
        init_par    = result

    print()
    print('Inital values for MCMC:')
    with os.popen('stty size', 'r') as rows_cols:
        cols            = rows_cols.read().split()[1]
        lnum            = (len(priors)*8)//(int(cols)-8) + 1
        priors_chunks   = np.array_split(np.array(prior_names), lnum)
        vals_chunks     = np.array_split([round(m_val, 3) for m_val in init_par], lnum)
        for pchunk, vchunk in zip(priors_chunks, vals_chunks):
            row_format ="{:>8}" * (len(pchunk) + 1)
            print(row_format.format("", *pchunk))
            print(row_format.format("", *vchunk))
            print()

    print()

    pos             = [init_par*(1+1e-3*np.random.randn(ndim)) for i in range(nwalkers)]
    sampler         = wrap_sampler(pos, nwalkers, ndim, ndraws, priors, ncores, update_freq, description, info)

    sampler.summary     = lambda: summary(sampler.chain[:,tune:,:], priors)
    sampler.traceplot   = lambda **args: traceplot(sampler.chain, varnames=priors, tune=tune, priors=priors_lst, **args)
    sampler.posteriorplot   = lambda **args: posteriorplot(sampler.chain, varnames=priors, tune=tune, **args)

    sampler.prior_dist  = priors_lst
    sampler.prior_names = prior_names
    sampler.tune        = tune
    par_mean            = par_fix
    par_mean[prior_arg] = mc_mean(sampler.chain[:,tune:], varnames=priors)
    sampler.par_means   = list(par_mean)

    self.sampler        = sampler
