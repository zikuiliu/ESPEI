"""
The paramselect module handles automated parameter selection for linear models.


Automated Parameter Selection
End-members

Note: All magnetic parameters from literature for now.
Note: No fitting below 298 K (so neglect third law issues for now).
Note: Should use calculate(..., mode='numpy') for this step for performance reasons.

For each step, add one parameter at a time and compute AIC with max likelihood.

Cp - TlnT, T**2, T**-1, T**3 - 4 candidate models
(S and H only have one required parameter each -- will fit in full MCMC procedure)

Choose parameter set with best AIC score.

4. G (full MCMC) - all parameters selected at least once by above procedure

Choice of priors:

(const.): Normal(mean=0, sd=1e6)
T:        Normal(mean=0, sd=50)
TlnT:     Normal(mean=0, sd=20)
T**2:     Normal(mean=0, sd=10)
T**-1:    Normal(mean=0, sd=1e6)
T**3:     Normal(mean=0, sd=5)

Should we use the ridge regression method instead of MCMC for selection?
If we use zero-mean normally-distributed priors, we just construct
a diagonal weighting matrix like 1/variance, and it's actually
equivalent. (But much, much faster to calculate.)
But then how do we compute a model score?
We compute AIC from 2k - 2ln(L_max), where L_max is max likelihood and k is
the number of parameters. All we need, then, is our likelihood estimate.
How do we get that from the regression result?
1. Use lstsq (or ridge) to get parameter values at max likelihood (min error).
2. Plug parameter values into likelihood function to get L_max.
3. Compute AIC.
4. Choose model with minimum AIC.

Looks straightforward: see Tikhonov regularization on Wikipedia
I think we're okay on parameter correlations if we build the matrix correctly

If we do pinv method like ZKL suggested, it's like doing Bayesian regression
with uninformative priors. With bias of AIC toward complex models, I think doing regularization
with ridge regression is advisible.
"""
import pycalphad.variables as v
from pycalphad import binplot, calculate, equilibrium, Database, Model
from pycalphad.plot.utils import phase_legend
import pycalphad.refdata
from sklearn.linear_model import LinearRegression
from sklearn.covariance import EmpiricalCovariance
import tinydb
import sympy
import numpy as np
import json
from collections import OrderedDict, defaultdict
import itertools
import operator
import copy
from functools import reduce


# Mapping of energy polynomial coefficients to corresponding property coefficients
feature_transforms = {"CPM_FORM": lambda x: -v.T*sympy.diff(x, v.T, 2),
                      "CPM": lambda x: -v.T*sympy.diff(x, v.T, 2),
                      "SM_FORM": lambda x: -sympy.diff(x, v.T),
                      "SM": lambda x: -sympy.diff(x, v.T),
                      "HM_FORM": lambda x: x - v.T*sympy.diff(x, v.T),
                      "HM": lambda x: x - v.T*sympy.diff(x, v.T)}

plot_mapping = {
    'T': 'Temperature (K)',
    'CPM': 'Heat Capacity (J/K-mol-atom)',
    'HM': 'Enthalpy (J/mol-atom)',
    'SM': 'Entropy (J/K-mol-atom)',
    'CPM_FORM': 'Heat Capacity of Formation (J/K-mol-atom)',
    'HM_FORM': 'Enthalpy of Formation (J/mol-atom)',
    'SM_FORM': 'Entropy of Formation (J/K-mol-atom)',
    'CPM_MIX': 'Heat Capacity of Mixing (J/K-mol-atom)',
    'HM_MIX': 'Enthalpy of Mixing (J/mol-atom)',
    'SM_MIX': 'Entropy of Mixing (J/K-mol-atom)'
}


def load_datasets(dataset_filenames):
    ds_database = tinydb.TinyDB(storage=tinydb.storages.MemoryStorage)
    for fname in dataset_filenames:
        with open(fname) as file_:
            try:
                ds_database.insert(json.load(file_))
            except ValueError as e:
                print('JSON Error in {}: {}'.format(fname, e))
    return ds_database


def canonical_sort_key(x):
    """
    Wrap strings in tuples so they'll sort.

    Parameters
    ==========
    x : sequence
    """
    return [tuple(i) if isinstance(i, (tuple, list)) else (i,) for i in x]


def canonicalize(configuration, equivalent_sublattices):
    """
    Sort a sequence with symmetry. This routine gives the sequence
    a deterministic ordering while respecting symmetry.

    Parameters
    ==========
    configuration : list
        Sublattice configuration to sort.
    equivalent_sublattices : set of set of int
        Indices of 'configuration' which should be equivalent by symmetry, i.e.,
        [[0, 4], [1, 2, 3]] means permuting elements 0 and 4, or 1, 2 and 3, respectively,
        has no effect on the equivalence of the sequence.

    Returns
    =======
    canonicalized : tuple
    """
    canonicalized = list(configuration)
    if equivalent_sublattices is not None:
        for subl in equivalent_sublattices:
            subgroup = sorted([configuration[idx] for idx in sorted(subl)], key=canonical_sort_key)
            for subl_idx, conf_idx in enumerate(sorted(subl)):
                if isinstance(subgroup[subl_idx], list):
                    canonicalized[conf_idx] = tuple(subgroup[subl_idx])
                else:
                    canonicalized[conf_idx] = subgroup[subl_idx]

    return _list_to_tuple(canonicalized)


def _symmetry_filter(x, config, symmetry):
    if x['mode'] == 'manual':
        if len(config) != len(x['sublattice_configurations'][0]):
            return False
        # If even one matches, it's a match
        # We do more filtering downstream
        for data_config in x['sublattice_configurations']:
            if canonicalize(config, symmetry) == canonicalize(data_config, symmetry):
                return True
    return False


def _get_data(comps, phase_name, configuration, symmetry, datasets, prop):
    configuration = list(configuration)
    desired_data = datasets.search((tinydb.where('output').test(lambda x: x in prop)) &
                                   (tinydb.where('components').test(lambda x: set(x).issubset(comps))) &
                                   (tinydb.where('solver').test(_symmetry_filter, configuration, symmetry)) &
                                   (tinydb.where('phases') == [phase_name]))
    # This seems to be necessary because the 'values' member does not modify 'datasets'
    # But everything else does!
    desired_data = copy.deepcopy(desired_data)
    #if len(desired_data) == 0:
    #    raise ValueError('No datasets for the system of interest containing {} were in \'datasets\''.format(prop))

    def recursive_zip(a, b):
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            return list(recursive_zip(x, y) for x, y in zip(a, b))
        else:
            return list(zip(a, b))

    for idx, data in enumerate(desired_data):
        # Filter output values to only contain data for matching sublattice configurations
        matching_configs = np.array([(canonicalize(sblconf, symmetry) == canonicalize(configuration, symmetry))
                                     for sblconf in data['solver']['sublattice_configurations']])
        matching_configs = np.arange(len(data['solver']['sublattice_configurations']))[matching_configs]
        # Rewrite output values with filtered data
        desired_data[idx]['values'] = np.array(data['values'], dtype=np.float)[..., matching_configs]
        desired_data[idx]['solver']['sublattice_configurations'] = _list_to_tuple(np.array(data['solver']['sublattice_configurations'],
                                                                            dtype=np.object)[matching_configs].tolist())
        try:
            desired_data[idx]['solver']['sublattice_occupancies'] = np.array(data['solver']['sublattice_occupancies'],
                                                                             dtype=np.object)[matching_configs].tolist()
        except KeyError:
            pass
        # Filter out temperatures below 298.15 K (for now, until better refstates exist)
        temp_filter = np.atleast_1d(data['conditions']['T']) >= 298.15
        desired_data[idx]['conditions']['T'] = np.atleast_1d(data['conditions']['T'])[temp_filter]
        # Don't use data['values'] because we rewrote it above; not sure what 'data' references now
        desired_data[idx]['values'] = desired_data[idx]['values'][..., temp_filter, :]
    return desired_data


def _fit_parameters(feature_matrix, data_quantities, feature_tuple):
    """
    Solve Ax = b, where 'feature_matrix' is A and 'data_quantities' is b.

    Parameters
    ==========
    feature_matrix : ndarray (M*N)
        Regressor matrix
    data_quantities : ndarray (M,)
        Response vector
    feature_tuple : tuple
        Polynomial coefficient corresponding to each column of 'feature_matrix'

    Returns
    =======
    parameters : OrderedDict
       Maps 'feature_tuple' to fitted parameter value.
       If a coefficient is not used, it maps to zero.
    """
    # Now generate candidate models; add parameters one at a time
    model_scores = []
    results = np.zeros((len(feature_tuple), len(feature_tuple)))
    clf = LinearRegression(fit_intercept=False, normalize=True)
    for num_params in range(1, feature_matrix.shape[-1] + 1):
        current_matrix = feature_matrix[:, :num_params]
        clf.fit(current_matrix, data_quantities)
        # This may not exactly be the correct form for the likelihood
        # We're missing the "ridge" contribution here which could become relevant for sparse data
        rss = np.square(np.dot(current_matrix, clf.coef_) - data_quantities.astype(np.float)).sum()
        # Compute Aikaike Information Criterion
        # Form valid under assumption all sample variances are equal and unknown
        score = 2*num_params + current_matrix.shape[-2] * np.log(rss)
        model_scores.append(score)
        results[num_params - 1, :num_params] = clf.coef_
        print(feature_tuple[:num_params], 'rss:', rss, 'AIC:', score)
    cov = EmpiricalCovariance(store_precision=False, assume_centered=False)
    cov.fit(feature_matrix[:, :np.argmin(model_scores)+1], data_quantities)
    print(cov.covariance_)
    return OrderedDict(zip(feature_tuple, results[np.argmin(model_scores), :]))


def _get_samples(desired_data):
    all_samples = []
    for data in desired_data:
        temperatures = np.atleast_1d(data['conditions']['T'])
        site_fractions = data['solver'].get('sublattice_occupancies', [[1]])
        site_fraction_product = [reduce(operator.mul, list(itertools.chain(*[np.atleast_1d(f) for f in fracs])), 1)
                                 for fracs in site_fractions]
        # TODO: Subtle sorting bug here, if the interactions aren't already in sorted order...
        interaction_product = []
        for fracs in site_fractions:
            interaction_product.append(float(reduce(operator.mul,
                                                    [f[0] - f[1] for f in fracs if isinstance(f, list) and len(f) == 2],
                                                    1)))
        if len(interaction_product) == 0:
            interaction_product = [0]
        comp_features = zip(site_fraction_product, interaction_product)
        all_samples.extend(list(itertools.product(temperatures, comp_features)))
    return all_samples


def _build_feature_matrix(prop, features, desired_data):
    transformed_features = sympy.Matrix([feature_transforms[prop](i) for i in features])
    all_samples = _get_samples(desired_data)
    feature_matrix = np.empty((len(all_samples), len(transformed_features)), dtype=np.float)
    feature_matrix[:, :] = [transformed_features.subs({v.T: temp, 'YS': compf[0],
                                                       'Z': compf[1]}).evalf()
                            for temp, compf in all_samples]
    return feature_matrix


def _endmembers_from_interaction(configuration):
    config = []
    for c in configuration:
        if isinstance(c, (list, tuple)):
            config.append(c)
        else:
            config.append([c])
    return list(itertools.product(*[tuple(c) for c in config]))


def _shift_reference_state(desired_data, feature_transform, fixed_model):
    "Shift data to a new common reference state."
    total_response = []
    for dataset in desired_data:
        values = np.asarray(dataset['values'], dtype=np.object)
        if dataset['solver'].get('sublattice_occupancies', None) is not None:
            value_idx = 0
            for occupancy, config in zip(dataset['solver']['sublattice_occupancies'],
                                         dataset['solver']['sublattice_configurations']):
                if dataset['output'].endswith('_FORM'):
                    pass
                elif dataset['output'].endswith('_MIX'):
                    values[..., value_idx] += feature_transform(fixed_model.models['ref'])
                    pass
                else:
                    raise ValueError('Unknown property to shift: {}'.format(dataset['output']))
                value_idx += 1
        total_response.append(values.flatten())
    return total_response


def sigfigs(x, n):
    if x != 0:
        return np.around(x, -(np.floor(np.log10(np.abs(x)))).astype(np.int) + (n - 1))
    else:
        return x


def fit_formation_energy(dbf, comps, phase_name, configuration, symmetry,
                         datasets, features=None):
    """
    Find suitable linear model parameters for the given phase.
    We do this by successively fitting heat capacities, entropies and
    enthalpies of formation, and selecting against criteria to prevent
    overfitting. The "best" set of parameters minimizes the error
    without overfitting.

    Parameters
    ==========
    dbf : Database
        Partially complete, so we know what degrees of freedom to fix.
    comps : list of str
        Names of the relevant components.
    phase_name : str
        Name of the desired phase for which the parameters will be found.
    configuration : ndarray
        Configuration of the sublattices for the fitting procedure.
    symmetry : set of set of int or None
        Symmetry of the sublattice configuration.
    datasets : tinydb of Dataset
        All the datasets desired to fit to.
    features : dict (optional)
        Maps "property" to a list of features for the linear model.
        These will be transformed from "GM" coefficients
        e.g., {"CPM_FORM": (v.T*sympy.log(v.T), v.T**2, v.T**-1, v.T**3)}

    Returns
    =======
    dict of feature: estimated value
    """
    if features is None:
        features = [("CPM_FORM", (v.T * sympy.log(v.T), v.T**2, v.T**-1, v.T**3)),
                    ("SM_FORM", (v.T,)),
                    ("HM_FORM", (sympy.S.One,))
                    ]
        features = OrderedDict(features)
    if any([isinstance(conf, (list, tuple)) for conf in configuration]):
        fitting_steps = (["CPM_FORM", "CPM_MIX"], ["SM_FORM", "SM_MIX"], ["HM_FORM", "HM_MIX"])
        # Product of all nonzero site fractions in all sublattices
        YS = sympy.Symbol('YS')
        # Product of all binary interaction terms
        Z = sympy.Symbol('Z')
        redlich_kister_features = (YS, YS*Z, YS*(Z**2), YS*(Z**3))
        for feature in features.keys():
            all_features = list(itertools.product(redlich_kister_features, features[feature]))
            features[feature] = [i[0]*i[1] for i in all_features]
        print('ENDMEMBERS FROM INTERACTION: '+str(_endmembers_from_interaction(configuration)))
    else:
        # We are only fitting an endmember; no mixing data needed
        fitting_steps = (["CPM_FORM"], ["SM_FORM"], ["HM_FORM"])

    parameters = {}
    for feature in features.values():
        for coef in feature:
            parameters[coef] = 0

    # These is our previously fit partial model
    # Subtract out all of these contributions (zero out reference state because these are formation properties)
    fixed_model = Model(dbf, comps, phase_name, parameters={'GHSER'+c.upper(): 0 for c in comps})
    fixed_model.models['idmix'] = 0
    fixed_portions = [0]

    moles_per_formula_unit = sympy.S(0)
    subl_idx = 0
    for num_sites, const in zip(dbf.phases[phase_name].sublattices, dbf.phases[phase_name].constituents):
        if 'VA' in const:
            moles_per_formula_unit += num_sites * (1 - v.SiteFraction(phase_name, subl_idx, 'VA'))
        else:
            moles_per_formula_unit += num_sites
        subl_idx += 1

    for desired_props in fitting_steps:
        desired_data = _get_data(comps, phase_name, configuration, symmetry, datasets, desired_props)
        print('{}: datasets found: {}'.format(desired_props, len(desired_data)))
        if len(desired_data) > 0:
            feature_matrix = _build_feature_matrix(desired_props[0], features[desired_props[0]], desired_data)
            all_samples = _get_samples(desired_data)
            data_quantities = np.concatenate(_shift_reference_state(desired_data,
                                                                    feature_transforms[desired_props[0]],
                                                                    fixed_model),
                                             axis=-1)
            site_fractions = [_build_sitefractions(phase_name, ds['solver']['sublattice_configurations'],
                ds['solver'].get('sublattice_occupancies',
                                 np.ones((len(ds['solver']['sublattice_configurations']),
                                          len(ds['solver']['sublattice_configurations'][0])), dtype=np.float)))
                              for ds in desired_data for _ in ds['conditions']['T']]
            # Flatten list
            site_fractions = list(itertools.chain(*site_fractions))
            # Remove existing partial model contributions from the data
            data_quantities = data_quantities - feature_transforms[desired_props[0]](fixed_model.ast)
            # Subtract out high-order (in T) parameters we've already fit
            data_quantities = data_quantities - \
                feature_transforms[desired_props[0]](sum(fixed_portions)) / moles_per_formula_unit
            for sf, i in zip(site_fractions, data_quantities):
                missing_variables = sympy.S(i * moles_per_formula_unit).atoms(v.SiteFraction) - set(sf.keys())
                sf.update({x: 0. for x in missing_variables})
            # moles_per_formula_unit factor is here because our data is stored per-atom
            # but all of our fits are per-formula-unit
            data_quantities = [sympy.S(i * moles_per_formula_unit).xreplace(sf).xreplace({v.T: ixx[0]}).evalf()
                               for i, sf, ixx in zip(data_quantities, site_fractions, all_samples)]
            data_quantities = np.asarray(data_quantities, dtype=np.float)
            parameters.update(_fit_parameters(feature_matrix, data_quantities, features[desired_props[0]]))
            # Add these parameters to be fixed for the next fitting step
            fixed_portion = np.array(features[desired_props[0]], dtype=np.object)
            fixed_portion = np.dot(fixed_portion, [parameters[feature] for feature in features[desired_props[0]]])
            fixed_portions.append(fixed_portion)

    return parameters


def _translate_endmember_to_array(endmember, variables):
    site_fractions = sorted(variables, key=str)
    frac_array = np.zeros(len(site_fractions))
    for idx, component in enumerate(endmember):
        frac_array[site_fractions.index(v.SiteFraction(site_fractions[0].phase_name, idx, component))] = 1
    return frac_array


def _build_sitefractions(phase_name, sublattice_configurations, sublattice_occupancies):
    """
    Convert nested lists of sublattice configurations and occupancies to a list of dictionaries.
    The dictionaries map SiteFraction symbols to occupancy values. Note that zero occupancy
    site fractions will need to be added separately since the total degrees of freedom aren't
    known in this function.
    :param phase_name:
    :param sublattice_configurations:
    :param sublattice_occupancies:
    :return:
    """
    result = []
    for config, occ in zip(sublattice_configurations, sublattice_occupancies):
        sitefracs = {}
        config = [[c] if not isinstance(c, (list, tuple)) else c for c in config]
        occ = [[o] if not isinstance(o, (list, tuple)) else o for o in occ]
        if len(config) != len(occ):
            raise ValueError('Sublattice configuration length differs from occupancies')
        for sublattice_idx in range(len(config)):
            if isinstance(config[sublattice_idx], (list, tuple)) != isinstance(occ[sublattice_idx], (list, tuple)):
                raise ValueError('Sublattice configuration type differs from occupancies')
            if not isinstance(config[sublattice_idx], (list, tuple)):
                # This sublattice is fully occupied by one component
                sitefracs[v.SiteFraction(phase_name, sublattice_idx, config[sublattice_idx])] = occ[sublattice_idx]
            else:
                # This sublattice is occupied by multiple elements
                if len(config[sublattice_idx]) != len(occ[sublattice_idx]):
                    raise ValueError('Length mismatch in sublattice configuration')
                for comp, val in zip(config[sublattice_idx], occ[sublattice_idx]):
                    sitefracs[v.SiteFraction(phase_name, sublattice_idx, comp)] = val
        result.append(sitefracs)
    return result


def _compare_data_to_parameters(dbf, comps, phase_name, desired_data, mod, configuration, x, y):
    import matplotlib.pyplot as plt
    all_samples = np.array(_get_samples(desired_data), dtype=np.object)
    endpoints = _endmembers_from_interaction(configuration)
    interacting_subls = [c for c in _list_to_tuple(configuration) if isinstance(c, tuple)]
    disordered_config = False
    if (len(set(interacting_subls)) == 1) and (len(interacting_subls[0]) == 2):
        # This configuration describes all sublattices with the same two elements interacting
        # In general this is a high-dimensional space; just plot the diagonal to see the disordered mixing
        endpoints = [endpoints[0], endpoints[-1]]
        disordered_config = True
    fig = plt.figure(figsize=(9, 9))
    bar_chart = False
    bar_labels = []
    bar_data = []
    if y.endswith('_FORM'):
        # We were passed a Model object with zeroed out reference states
        yattr = y[:-5]
    else:
        yattr = y
    if len(endpoints) == 1:
        # This is an endmember so we can just compute T-dependent stuff
        temperatures = np.array([i[0] for i in all_samples], dtype=np.float)
        if temperatures.min() != temperatures.max():
            temperatures = np.linspace(temperatures.min(), temperatures.max(), num=100)
        else:
            # We only have one temperature: let's do a bar chart instead
            bar_chart = True
            temperatures = temperatures.min()
        endmember = _translate_endmember_to_array(endpoints[0], mod.ast.atoms(v.SiteFraction))[None, None]
        predicted_quantities = calculate(dbf, comps, [phase_name], output=yattr,
                                         T=temperatures, P=101325, points=endmember, model=mod, mode='numpy')
        if y == 'HM' and x == 'T':
            # Shift enthalpy data so that value at minimum T is zero
            predicted_quantities[yattr] -= predicted_quantities[yattr].sel(T=temperatures[0]).values.flatten()
        response_data = predicted_quantities[yattr].values.flatten()
        if not bar_chart:
            extra_kwargs = {}
            if len(response_data) < 10:
                extra_kwargs['markersize'] = 20
                extra_kwargs['marker'] = '.'
                extra_kwargs['linestyle'] = 'none'
                extra_kwargs['clip_on'] = False
            fig.gca().plot(temperatures, response_data,
                           label='This work', color='k', **extra_kwargs)
            fig.gca().set_xlabel(plot_mapping.get(x, x))
            fig.gca().set_ylabel(plot_mapping.get(y, y))
        else:
            bar_labels.append('This work')
            bar_data.append(response_data[0])
    elif len(endpoints) == 2:
        # Binary interaction parameter
        first_endpoint = _translate_endmember_to_array(endpoints[0], mod.ast.atoms(v.SiteFraction))
        second_endpoint = _translate_endmember_to_array(endpoints[1], mod.ast.atoms(v.SiteFraction))
        point_matrix = np.linspace(0, 1, num=100)[None].T * second_endpoint + \
            (1 - np.linspace(0, 1, num=100))[None].T * first_endpoint
        # TODO: Real temperature support
        point_matrix = point_matrix[None, None]
        predicted_quantities = calculate(dbf, comps, [phase_name], output=yattr,
                                         T=300, P=101325, points=point_matrix, model=mod, mode='numpy')
        response_data = predicted_quantities[yattr].values.flatten()
        if not bar_chart:
            extra_kwargs = {}
            if len(response_data) < 10:
                extra_kwargs['markersize'] = 20
                extra_kwargs['marker'] = '.'
                extra_kwargs['linestyle'] = 'none'
                extra_kwargs['clip_on'] = False
            fig.gca().plot(np.linspace(0, 1, num=100), response_data,
                           label='This work', color='k', **extra_kwargs)
            fig.gca().set_xlim((0, 1))
            fig.gca().set_xlabel(str(':'.join(endpoints[0])) + ' to ' + str(':'.join(endpoints[1])))
            fig.gca().set_ylabel(plot_mapping.get(y, y))
        else:
            bar_labels.append('This work')
            bar_data.append(response_data[0])
    else:
        raise NotImplementedError('No support for plotting configuration {}'.format(configuration))

    for data in desired_data:
        indep_var_data = None
        response_data = np.zeros_like(data['values'], dtype=np.float)
        if x == 'T' or x == 'P':
            indep_var_data = np.array(data['conditions'][x], dtype=np.float).flatten()
        elif x == 'Z':
            if disordered_config:
                # Take the second element of the first interacting sublattice as the coordinate
                # Because it's disordered all sublattices should be equivalent
                # TODO: Fix this to filter because we need to guarantee the plot points are disordered
                occ = data['solver']['sublattice_occupancies']
                subl_idx = np.nonzero([isinstance(c, (list, tuple)) for c in occ[0]])[0]
                if len(subl_idx) > 1:
                    subl_idx = int(subl_idx[0])
                else:
                    subl_idx = int(subl_idx)
                indep_var_data = [c[subl_idx][1] for c in occ]
            else:
                interactions = np.array([i[1][1] for i in _get_samples([data])], dtype=np.float)
                indep_var_data = 1 - (interactions+1)/2
            if y.endswith('_MIX') and data['output'].endswith('_FORM'):
                # All the _FORM data we have still has the lattice stability contribution
                # Need to zero it out to shift formation data to mixing
                mod_latticeonly = Model(dbf, comps, phase_name, parameters={'GHSER'+c.upper(): 0 for c in comps})
                mod_latticeonly.models = {key: value for key, value in mod_latticeonly.models.items()
                                          if key == 'ref'}
                temps = data['conditions'].get('T', 300)
                pressures = data['conditions'].get('P', 101325)
                points = _build_sitefractions(phase_name, data['solver']['sublattice_configurations'],
                                              data['solver']['sublattice_occupancies'])
                for point_idx in range(len(points)):
                    missing_variables = mod_latticeonly.ast.atoms(v.SiteFraction) - set(points[point_idx].keys())
                    # Set unoccupied values to zero
                    points[point_idx].update({key: 0 for key in missing_variables})
                    # Change entry to a sorted array of site fractions
                    points[point_idx] = list(OrderedDict(sorted(points[point_idx].items(), key=str)).values())
                points = np.array(points, dtype=np.float)
                # TODO: Real temperature support
                points = points[None, None]
                stability = calculate(dbf, comps, [phase_name], output=data['output'][:-5],
                                      T=temps, P=pressures, points=points,
                                      model=mod_latticeonly, mode='numpy')
                response_data -= stability[data['output'][:-5]].values

        response_data += np.array(data['values'], dtype=np.float)
        response_data = response_data.flatten()
        if not bar_chart:
            extra_kwargs = {}
            if len(response_data) < 10:
                extra_kwargs['markersize'] = 20
                extra_kwargs['marker'] = '.'
                extra_kwargs['linestyle'] = 'none'
                extra_kwargs['clip_on'] = False

            fig.gca().plot(indep_var_data, response_data, label=data.get('reference', None),
                           **extra_kwargs)
        else:
            bar_labels.append(data.get('reference', None))
            bar_data.append(response_data[0])
    if bar_chart:
        fig.gca().barh(0.02 * np.arange(len(bar_data)), bar_data,
                       color='k', height=0.01)
        endmember_title = ' to '.join([':'.join(i) for i in endpoints])
        fig.suptitle('{} (T = {} K)'.format(endmember_title, temperatures), fontsize=20)
        fig.gca().set_yticks(0.02 * np.arange(len(bar_data)))
        fig.gca().set_yticklabels(bar_labels, fontsize=20)
        # This bar chart is rotated 90 degrees, so "y" is now x
        fig.gca().set_xlabel(plot_mapping.get(y, y))
    else:
        fig.gca().set_frame_on(False)
        leg = fig.gca().legend(loc='best')
        leg.get_frame().set_edgecolor('black')
    fig.canvas.draw()


def plot_parameters(dbf, comps, phase_name, configuration, symmetry, datasets=None):
    em_plots = [('T', 'CPM'), ('T', 'CPM_FORM'), ('T', 'SM'), ('T', 'SM_FORM'),
                ('T', 'HM'), ('T', 'HM_FORM')]
    mix_plots = [('Z', 'HM_FORM'), ('Z', 'HM_MIX'), ('Z', 'SM_MIX')]
    comps = sorted(comps)
    mod = Model(dbf, comps, phase_name)
    # This is for computing properties of formation
    mod_norefstate = Model(dbf, comps, phase_name, parameters={'GHSER'+c.upper(): 0 for c in comps})
    # Is this an interaction parameter or endmember?
    if any([isinstance(conf, list) or isinstance(conf, tuple) for conf in configuration]):
        plots = mix_plots
    else:
        plots = em_plots
    for x_val, y_val in plots:
        if datasets is not None:
            if y_val.endswith('_MIX'):
                desired_props = [y_val.split('_')[0]+'_FORM', y_val]
            else:
                desired_props = [y_val]
            desired_data = _get_data(comps, phase_name, configuration, symmetry, datasets, desired_props)
        else:
            desired_data = []
        if len(desired_data) == 0:
            continue
        if y_val.endswith('_FORM'):
            _compare_data_to_parameters(dbf, comps, phase_name, desired_data, mod_norefstate, configuration, x_val, y_val)
        else:
            _compare_data_to_parameters(dbf, comps, phase_name, desired_data, mod, configuration, x_val, y_val)


def _list_to_tuple(x):
    def _tuplify(y):
        if isinstance(y, list) or isinstance(y, tuple):
            return tuple(_tuplify(i) if isinstance(i, (list, tuple)) else i for i in y)
        else:
            return y
    return tuple(map(_tuplify, x))


def _tuple_to_list(x):
    def _listify(y):
        if isinstance(y, list) or isinstance(y, tuple):
            return list(y)
        else:
            return y
    return list(map(_listify, x))


def _generate_symmetric_group(configuration, symmetry):
    configurations = [_list_to_tuple(configuration)]
    permutation = np.array(symmetry, dtype=np.object)

    def permute(x):
        if len(x) == 0:
            return x
        x[0] = np.roll(x[0], 1)
        x[:] = np.roll(x, 1, axis=0)
        return x

    if symmetry is not None:
        while np.any(np.array(symmetry, dtype=np.object) != permute(permutation)):
            new_conf = np.array(configurations[0], dtype=np.object)
            subgroups = []
            # There is probably a more efficient way to do this
            for subl in permutation:
                subgroups.append([configuration[idx] for idx in subl])
            # subgroup is ordered according to current permutation
            # but we'll index it based on the original symmetry
            # This should permute the configurations
            for subl, subgroup in zip(symmetry, subgroups):
                for subl_idx, conf_idx in enumerate(subl):
                    new_conf[conf_idx] = subgroup[subl_idx]
            configurations.append(tuple(new_conf))

    return sorted(set(configurations), key=canonical_sort_key)


def phase_fit(dbf, phase_name, symmetry, subl_model, site_ratios, datasets, refdata, aliases=None):
    """
    Generate an initial CALPHAD model for a given phase and
    sublattice model.

    Parameters
    ==========
    dbf : Database
        Database to add parameters to.
    phase_name : str
        Name of the phase.
    symmetry : set of set of int or None
        Sublattice model symmetry.
    subl_model : list of tuple
        Sublattice model for the phase of interest.
    site_ratios : list of float
        Number of sites in each sublattice, normalized to one atom.
    datasets : tinydb of datasets
        All datasets to consider for the calculation.
    refdata : dict
        Maps tuple(element, phase_name) -> SymPy object defining energy relative to SER
    aliases : list or None
        Alternative phase names. Useful for matching against reference data or other datasets.
    """
    # First fit endmembers
    all_em_count = len(list(itertools.product(*subl_model)))
    endmembers = sorted(set(canonicalize(i, symmetry) for i in itertools.product(*subl_model)))
    # Number of significant figures in parameters
    numdigits = 6
    em_dict = {}
    aliases = [] if aliases is None else aliases
    aliases = sorted(set(aliases + [phase_name]))
    print('FITTING: ', phase_name)
    print('{0} endmembers ({1} distinct by symmetry)'.format(all_em_count, len(endmembers)))

    def _to_tuple(x):
        if isinstance(x, list) or isinstance(x, tuple):
            return tuple(x)
        else:
            return tuple([x])

    all_endmembers = []
    for endmember in endmembers:
        print('ENDMEMBER: '+str(endmember))
        # Some endmembers are fixed by our choice of standard lattice stabilities, e.g., SGTE91
        # If a (phase, pure component endmember) tuple is fixed, we should use that value instead of fitting
        endmember_comps = list(set(endmember))
        fit_eq = None
        # only one non-VA component, or two components but the other is VA and its only the last sublattice
        if ((len(endmember_comps) == 1) and (endmember_comps[0] != 'VA')) or\
                ((len(endmember_comps) == 2) and (endmember[-1] == 'VA') and (len(set(endmember[:-1])) == 1)):
            # this is a "pure component endmember"
            # try all phase name aliases until we get run out or get a hit
            em_comp = list(set(endmember_comps) - {'VA'})[0]
            sym_name = None
            for name in aliases:
                sym_name = 'G'+name[:3].upper()+em_comp.upper()
                stability = refdata.get((em_comp.upper(), name.upper()), None)
                if stability is not None:
                    dbf.symbols[sym_name] = stability
                    break
            if dbf.symbols.get(sym_name, None) is not None:
                num_moles = sum([sites for elem, sites in zip(endmember, site_ratios) if elem != 'VA'])
                fit_eq = num_moles * sympy.Symbol(sym_name)
        if fit_eq is None:
            # No reference lattice stability data -- we have to fit it
            parameters = fit_formation_energy(dbf, sorted(dbf.elements), phase_name, endmember, symmetry, datasets)
            fit_eq = sympy.Add(*[sigfigs(value, numdigits) * key for key, value in parameters.items()])
            ref = 0
            for subl, ratio in zip(endmember, site_ratios):
                if subl == 'VA':
                    continue
                ref = ref + ratio * sympy.Symbol('GHSER'+subl)
            fit_eq += ref
        symmetric_endmembers = _generate_symmetric_group(endmember, symmetry)
        print('SYMMETRIC_ENDMEMBERS: ', symmetric_endmembers)
        all_endmembers.extend(symmetric_endmembers)
        for em in symmetric_endmembers:
            em_dict[em] = fit_eq
            dbf.add_parameter('G', phase_name, tuple(map(_to_tuple, em)), 0, fit_eq)
    # Now fit all binary interactions
    # Need to use 'all_endmembers' instead of 'endmembers' because you need to generate combinations
    # of ALL endmembers, not just symmetry equivalent ones
    bin_interactions = list(itertools.combinations(all_endmembers, 2))
    transformed_bin_interactions = []
    for first_endmember, second_endmember in bin_interactions:
        interaction = []
        for first_occupant, second_occupant in zip(first_endmember, second_endmember):
            if first_occupant == second_occupant:
                interaction.append(first_occupant)
            else:
                interaction.append(tuple(sorted([first_occupant, second_occupant])))
        transformed_bin_interactions.append(interaction)

    def bin_int_sort_key(x):
        interacting_sublattices = sum((isinstance(n, (list, tuple)) and len(n) == 2) for n in x)
        return canonical_sort_key((interacting_sublattices,) + x)

    bin_interactions = sorted(set(canonicalize(i, symmetry) for i in transformed_bin_interactions),
                              key=bin_int_sort_key)
    print('{0} distinct binary interactions'.format(len(bin_interactions)))
    for interaction in bin_interactions:
        ixx = []
        for i in interaction:
            if isinstance(i, (tuple, list)):
                ixx.append(tuple(i))
            else:
                ixx.append(i)
        ixx = tuple(ixx)
        print('INTERACTION: '+str(ixx))
        parameters = fit_formation_energy(dbf, sorted(dbf.elements), phase_name, ixx, symmetry, datasets)
        # Organize parameters by polynomial degree
        degree_polys = np.zeros(10, dtype=np.object)
        for degree in reversed(range(10)):
            check_symbol = sympy.Symbol('YS') * sympy.Symbol('Z')**degree
            keys_to_remove = []
            for key, value in parameters.items():
                if key.has(check_symbol):
                    coef = sigfigs(parameters[key], numdigits) * (key / check_symbol)
                    # Try converting to float to work around a print precision problem with sympy
                    try:
                        coef = float(coef)
                    except TypeError:
                        pass
                    degree_polys[degree] += coef
                    keys_to_remove.append(key)
            for key in keys_to_remove:
                parameters.pop(key)
        print(degree_polys)
        # Insert into database
        symmetric_interactions = _generate_symmetric_group(interaction, symmetry)
        for degree in np.arange(degree_polys.shape[0]):
            if degree_polys[degree] != 0:
                for syminter in symmetric_interactions:
                    dbf.add_parameter('L', phase_name, tuple(map(_to_tuple, syminter)), degree, degree_polys[degree])
    # Now fit ternary interactions


def multi_plot(dbf, comps, phases, datasets, ax=None):
    import matplotlib.pyplot as plt
    plots = [('ZPF', 'T')]
    real_components = sorted(set(comps) - {'VA'})
    legend_handles, phase_color_map = phase_legend(phases)
    for output, indep_var in plots:
        desired_data = datasets.search((tinydb.where('output') == output) &
                                       (tinydb.where('components').test(lambda x: set(x).issubset(comps))) &
                                       (tinydb.where('phases').test(lambda x: len(set(phases).intersection(x)) > 0)))
        ax = ax if ax is not None else plt.gca()
        # TODO: There are lot of ways this could break in multi-component situations
        chosen_comp = real_components[-1]
        ax.set_xlabel('X({})'.format(chosen_comp))
        ax.set_ylabel(indep_var)
        ax.set_xlim((0, 1))
        symbol_map = {1: "o", 2: "s", 3: "^"}
        for data in desired_data:
            payload = data['values']
            # TODO: Add broadcast_conditions support
            # Repeat the temperature (or whatever variable) vector to align with the unraveled data
            temp_repeats = np.zeros(len(np.atleast_1d(data['conditions'][indep_var])), dtype=np.int)
            for idx, x in enumerate(payload):
                temp_repeats[idx] = len(x)
            temps_ravelled = np.repeat(data['conditions'][indep_var], temp_repeats)
            payload_ravelled = []
            phases_ravelled = []
            comps_ravelled = []
            symbols_ravelled = []
            # TODO: Fix to only include equilibria listed in 'phases'
            for p in payload:
                symbols_ravelled.extend([symbol_map[len(p)]] * len(p))
                payload_ravelled.extend(p)
            for rp in payload_ravelled:
                phases_ravelled.append(rp[0])
                comp_dict = dict(zip([x.upper() for x in rp[1]], rp[2]))
                dependent_comp = list(set(real_components) - set(comp_dict.keys()))
                if len(dependent_comp) > 1:
                    raise ValueError('Dependent components greater than one')
                elif len(dependent_comp) == 1:
                    dependent_comp = dependent_comp[0]
                    # TODO: Assuming N=1
                    comp_dict[dependent_comp] = 1 - sum(np.array(list(comp_dict.values()), dtype=np.float))
                chosen_comp_value = comp_dict[chosen_comp]
                comps_ravelled.append(chosen_comp_value)
            symbols_ravelled = np.array(symbols_ravelled)
            comps_ravelled = np.array(comps_ravelled)
            temps_ravelled = np.array(temps_ravelled)
            phases_ravelled = np.array(phases_ravelled)
            # We can't pass an array of markers to scatter, sadly
            for sym in symbols_ravelled:
                selected = symbols_ravelled == sym
                ax.scatter(comps_ravelled[selected], temps_ravelled[selected], marker=sym, s=100,
                           c='none', edgecolors=[phase_color_map[x] for x in phases_ravelled[selected]])
        ax.legend(handles=legend_handles, loc='center left', bbox_to_anchor=(1, 0.5))


def multi_phase_fit(dbf, comps, phases, datasets):
    desired_data = datasets.search((tinydb.where('output') == 'ZPF') &
                                   (tinydb.where('components').test(lambda x: set(x).issubset(comps))) &
                                   (tinydb.where('phases').test(lambda x: len(set(phases).intersection(x)) > 0)))
    for data in desired_data:
        payload = data['values']
        conditions = data['conditions']
        broadcast = data.get('broadcast_conditions', False)
        print(conditions)
        phase_regions = defaultdict(lambda: defaultdict(lambda: list()))
        # We send the equilibrium calculation to be performed all at once for each phase tuple
        # We have to track which calculations need to be compared against each other
        equilibria_indices = defaultdict(lambda: list())
        phase_region_indices = defaultdict(lambda: list())
        # TODO: Fix to only include equilibria listed in 'phases'
        for idx, p in enumerate(payload):
            phase_key = tuple(sorted(rp[0] for rp in p))
            if len(phase_key) < 2:
                # Skip single-phase regions for fitting purposes
                continue
            comp_dicts = [dict(zip([x.upper() for x in rp[1]], rp[2])) for rp in p]
            for cd_idx, cd in enumerate(comp_dicts):
                for dcomp, compval in cd.items():
                    compval = compval if compval is not None else np.nan
                    phase_regions[phase_key][v.X(dcomp.upper())].append(compval)
            # Need to not just check the chemical potential difference
            # Need to also verify that they are still zero phase-fraction lines
            equilibria_indices[phase_key].append(list(range(len(phase_key))))
            phase_region_indices[phase_key].append(idx)
        for region, comp_dict in phase_regions.items():
            print(region)
            print(phase_region_indices[region])
            region_conds = OrderedDict()
            for key, value in conditions.items():
                value = np.atleast_1d(np.asarray(value))
                if len(value) == 1:
                    region_conds[getattr(v, key)] = np.atleast_1d(np.asarray(value))
                else:
                    value = np.repeat(value[phase_region_indices[region]], [len(x) for x in equilibria_indices[region]])
                    region_conds[getattr(v, key)] = value
            region_conds.update(comp_dict)
            print(region_conds)
            region_keys = list(region_conds.keys())
            cond_size = max(len(x) for x in region_conds.values())
            region_cond_vals = []
            for val in region_conds.values():
                if len(val) > 1:
                    region_cond_vals.append(val)
                else:
                    region_cond_vals.append(np.repeat(val, cond_size))
            region_eq = np.array(region_cond_vals, dtype=np.float).T
            for req in region_eq:
                current_conds = dict(zip(region_keys, req))
                print(current_conds)
                if np.any(np.isnan(list(current_conds.values()))):
                    pass
                else:
                    # Extract chemical potential hyperplane from multi-phase calculation
                    multi_eqdata = equilibrium(dbf, comps, list(region), current_conds, pbar=False, verbose=True)
                    print(multi_eqdata.MU)
            for req, current_phase in zip(region_eq, region):
                current_conds = dict(zip(region_keys, req))
                print(current_conds)
                if np.any(np.isnan(list(current_conds.values()))):
                    pass
                else:
                    # Extract energies from single-phase calculations
                    single_eqdata = equilibrium(dbf, comps, current_phase, current_conds, pbar=False, verbose=True)
                    print(single_eqdata.GM)


def fit(input_fname, datasets):
    # TODO: Validate input JSON
    data = json.load(open(input_fname))
    dbf = Database()
    dbf.elements = sorted(data['components'])
    # Write reference state to Database
    refdata = getattr(pycalphad.refdata, data['refdata'])
    stabledata = getattr(pycalphad.refdata, data['refdata']+'Stable')
    comp_refs = {c.upper(): stabledata[c.upper()] for c in dbf.elements if c.upper() != 'VA'}
    comp_refs['VA'] = 0
    dbf.symbols.update({'GHSER'+c.upper(): data for c, data in comp_refs.items()})
    for phase_name, phase_obj in data['phases'].items():
        # Perform parameter selection and single-phase fitting based on input
        # TODO: Need to pass particular models to include: magnetic, order-disorder, etc.
        symmetry = phase_obj.get('equivalent_sublattices', None)
        aliases = phase_obj.get('aliases', None)
        # TODO: More advanced phase data searching
        site_ratios = phase_obj['sublattice_site_ratios']
        subl_model = phase_obj['sublattice_model']
        dbf.add_phase(phase_name, {}, site_ratios)
        dbf.add_phase_constituents(phase_name, subl_model)
        # phase_fit() adds parameters to dbf
        phase_fit(dbf, phase_name, symmetry, subl_model, site_ratios, datasets, refdata, aliases=aliases)
    # TODO: Fitting with multi-phase data
    # Do I include experimental data for the first time here, or above in single-phase fit?
    # Do I only fit new degrees of freedom here, or do I refine the existing ones? If the latter, which ones?
    return dbf
