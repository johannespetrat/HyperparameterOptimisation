import numpy as np
import warnings

import sklearn.gaussian_process as gp
from scipy.optimize import minimize
from scipy.stats import norm

from optml.optimizer_base import Optimizer, MissingValueException
from optml.bayesian_optimizer.kernels import HammingKernel, WeightedHammingKernel
from optml.bayesian_optimizer.optimizers import MixedAnnealer, CategoricalMaximizer, cartesian_product
from sklearn.gaussian_process.kernels import Matern

from optml.bayesian_optimizer.gp_categorical import GaussianProcessRegressorWithCategorical

class BayesianOptimizer(Optimizer):
    """ Bayesian Optimizer
    Implemented as described in the paper 'Practical Bayesian Optimization of Machine 
    Learning Algorithms' (https://arxiv.org/abs/1206.2944)

    For categorical parameters the optimizer used a WeightedHammingKernel as described in
    'Sequential Model-Based Optimization for General Algorithm Configuration' by 
    Frank Hutter, Holger H. Hoos, Kevin Leyton-Brown doi:10.1007/978-3-642-25566-3_40

    Args:
        model: a model (currently supports scikit-learn, xgboost, or a class 
               derived from optml.models.Model)
        hyperparams: a list of Parameter instances
        eval_func: score function to be maximized. Takes input (y_true, y_predicted) where 
            y_true and y_predicted are numpy arrays

    Attributes:
        model: a model (currently supports scikit-learn, xgboost, or a class 
               derived from optml.models.Model)
        hyperparam_history: a list of dictionaries with parameters and scores
        hyperparams: the list of parameters that the model is optimized over
        eval_func: loss function to be minimized
        model_module: can be 'sklearn', 'pipeline', 'xgboost_sklearn', 'keras' or user-defined model
        param_dict: dictionary where key=parameter name and value is the Parameter instance
        n_restart_optimizer: number of retries if maximization of acquisition function is 
            unsuccessful
        eval_func: loss function to be minimized. Takes input (y_true, y_predicted) where 
            y_true and y_predicted are numpy arrays
        bounds_arr: a Nx2 numpy array giving lower and upper bounds of all numeric 
            parameters. N = #hyperparameters to optimize
        success: Flag indicating whether acquisition function could successfully be maximized
        acquisition_function: 
    """
    def __init__(self, model, hyperparams, eval_func, start_vals=None, 
                 acquisition_function='expected_improvement',
                 n_restarts_optimizer=10, normalize=False,
                 exploration_control=0.01):
        super(BayesianOptimizer, self).__init__(model, hyperparams, eval_func, start_vals)
        self.get_type_of_optimization()
        self.kernel = self.choose_kernel()
        self.n_restarts_optimizer = n_restarts_optimizer
        self.eval_func = eval_func
        self.set_hyperparam_bounds()
        self.success = None
        self.normalize = normalize
        self.acquisition_function = acquisition_function
        if acquisition_function == 'generalized_expected_improvement':
            self.exploration_control = exploration_control

    def choose_kernel(self):
        """
        Selects a kernel depending on the type of optimization problem. 
            - Hamming kernel for problems with only categorical hyperparameters
            - Weighted Hamming kernel for problems with mixed categorical and numerical hyperparameters
            - Matern kernel for problems with only numerical hyperparameters

        Args:
            None

        Returns:
            a kernel that is compatible with kernels in sklearn.gaussian_process.kernels
        """
        n_categorical = np.sum([hp.param_type=='categorical' for hp in self.hyperparams])
        if self.optimization_type == 'categorical':
            kernel = HammingKernel()
        elif self.optimization_type == 'mixed':
            param_types = ['categorical' if hp.param_type=='categorical' else 'numeric' for hp in self.hyperparams]
            kernel = WeightedHammingKernel()
        else:
            kernel = Matern()
        return kernel

    def get_type_of_optimization(self):
        """
        Evaluates the type of optimization problem.

        Args:
            None
        Returns:
            A string with either 'categorical', 'mixed' or 'numerical'

        """
        n_categorical = np.sum([hp.param_type=='categorical' for hp in self.hyperparams])
        if n_categorical == len(self.hyperparams):
            self.optimization_type = 'categorical'
        elif n_categorical>0:
            self.optimization_type = 'mixed'
        else:
            self.optimization_type = 'numerical'

    def set_hyperparam_bounds(self):
        """
        Sets the lower and upper limits for each numerical hyperparameter. 

        Args:
            None

        Returns:
            None
        """
        self.bounds_arr = np.array([[hp.lower, hp.upper] for hp in self.hyperparams if hp.param_type!='categorical'])

    def add_bounds_for_categorical(self, bounds_arr):
        """
        not used
        """
        for param in self.hyperparams:
            if param.param_type == 'categorical':
                lower = np.zeros(len(param.possible_values))
                upper = np.ones(len(param.possible_values))
                bounds_arr = np.concatenate([bounds_arr, np.vstack([lower, upper]).T])
        return bounds_arr

    def upper_confidence_bound(self, optimizer, x):
        """
        Calculates the upper confidence bound as an acquisition function.

        Args:
            optimizer: a fitted gaussian process regressor
            x: a numpy array with parameter values
        Returns:
            a float
        """
        mu,std = optimizer.predict(np.atleast_2d(x), return_std=True)
        return (mu+1.96*std)[0]

    def expected_improvement(self, optimizer, x):
        """
        Calculates the expected improvement as an acquisition function.

        Args:
            optimizer: a fitted gaussian process regressor
            x: a numpy array with parameter values
        Returns:
            a float
        """
        mu, std = optimizer.predict(np.atleast_2d(x), return_std=True)
        current_best = max([score for score, params in self.hyperparam_history])
        if std == 0:
            return 0
        else:
            gamma = (mu[0] - current_best)/std[0]
            exp_improv = std[0] * (gamma * norm.cdf(gamma) + norm.pdf(gamma))
            return exp_improv

    def generalized_expected_improvement(self, optimizer, x, xi=0.01):
        """
        Calculates the generalized expected improvement as an acquisition function
        following the definition in https://arxiv.org/pdf/1012.2599 (page 14)

        Args:
            optimizer: a fitted gaussian process
            x: a numpy array with parameter values
            xi: controls the trade-off between exploration and exploitation. default is 0.01
                which is suggested in the paper
        Returns:
            a float
        """
        mu,std = optimizer.predict(np.atleast_2d(x), return_std=True)
        if std == 0:
            return 0
        else:
            current_best = max([score for score, params in self.hyperparam_history])
            gamma = (mu[0] - current_best - xi)/std[0]
            exp_improv = (mu[0] - current_best - xi) * norm.cdf(gamma) + std[0] * norm.pdf(gamma)
            return exp_improv

    def probability_of_improvement(self, optimizer, x):
        """
        Calculates the probability of improvement as an acquisition function
    
        Args:
            optimizer: a fitted gaussian process
            x: a numpy array with parameter values
        Returns:
            a float
        """
        mu,std = optimizer.predict(np.atleast_2d(x), return_std=True)
        current_best = max([score for score, params in self.hyperparam_history])
        if std == 0:
            return 0
        else:
            gamma = (mu[0] - current_best)/std[0]
            return norm.cdf(gamma)


    def optimize_continuous_problem(self, optimizer, start_vals):
        """
        Maximizes the acquisition function for problems with only continuous hyperparameters.
        The optimization method used is L-BFGS-B.
        Note that the maximization problem is converted to a minimization problem so that the 
        function scipy.optimize.minimize can be applied.

        Args:
            optimizer: a fitted gaussian process regressor
            start_vals: a numpy array with start values for the hyperparameters. Note that the
                        order is assumed to be the same as in self.hyperparams

        Returns:
            a dictionary with a flag indicating success of the optimization and the 
            resulting hyperparameter values
        """
        if self.acquisition_function == 'expected_improvement':
            minimized = minimize(lambda x: -1 * self.expected_improvement(optimizer, x), start_vals, bounds=self.bounds_arr, method='L-BFGS-B')
        elif self.acquisition_function == 'upper_confidence_bound':
            minimized = minimize(lambda x: -1 * self.upper_confidence_bound(optimizer, x), start_vals, bounds=self.bounds_arr, method='L-BFGS-B')
        elif self.acquisition_function == 'probability_of_improvement':
            minimized = minimize(lambda x: -1 * self.probability_of_improvement(optimizer, x), start_vals, bounds=self.bounds_arr, method='L-BFGS-B')
        elif self.acquisition_function == 'generalized_expected_improvement':
            minimized = minimize(lambda x: -1 * self.generalized_expected_improvement(optimizer, x, self.exploration_control), start_vals, bounds=self.bounds_arr, method='L-BFGS-B')
        return minimized

    def optimize_categorical_problem(self, optimizer, start_vals):
        """
        Maximizes the acquisition function for problems with only categorical hyperparameters.

        Args:
            optimizer: a fitted gaussian process regressor
            start_vals: a numpy array with start values for the hyperparameters. Note that the
                        order is assumed to be the same as in self.hyperparams

        Returns:
            a dictionary with a flag indicating success of the optimization and the 
            resulting hyperparameter values
        """
        param_grid = [np.array(p.possible_values) for p in self.hyperparams]
        n_combinations = len(cartesian_product(*param_grid))
        if n_combinations > 1000:
            annealer = MixedAnnealer(self, optimizer)
            result = annealer.anneal()
            if np.isnan(result[1]):
                success = False
            else:
                success = True
            max_vals = result[0]
        else:
            maximizer = CategoricalMaximizer(self, optimizer)
            max_vals = maximizer.find_max()
            success = True
        minimized = {'success': success,
                     'x': self._param_dict_to_arr(max_vals)}
        return minimized


    def optimize_mixed_problem(self, optimizer, start_vals):
        """
        Maximizes the acquisition function for problems with mixed types of hyperparameters.
        The optimization method used is Simulated Annealing.

        Args:
            optimizer: a fitted gaussian process regressor
            start_vals: a numpy array with start values for the hyperparameters. Note that the
                        order is assumed to be the same as in self.hyperparams

        Returns:
            a dictionary with a flag indicating success of the optimization and the 
            resulting hyperparameter values
        """
        annealer = MixedAnnealer(self, optimizer)
        result = annealer.anneal()
        if np.isnan(result[1]):
            success = False
        else:
            success = True
        minimized = {'success': success,
                     'x': self._param_dict_to_arr(result[0])}
        return minimized

    def get_next_hyperparameters(self, optimizer):
        """
        For a set of scores with hyperparameters and a fitted gaussian process regressor
        find the hyperparameter values that maximise the acquisition function.

        Args:
            optimizer: a fitted gaussian process regressor

        Returns:
            a dictionary with parameter names as keys and parameter values as values
        """
        best_params = {}
        for i in range(self.n_restarts_optimizer):
            if self.start_vals is None:
                start_vals = self.get_random_values_arr()
            else:
                start_vals = self.get_default_values_arr()
            if self.optimization_type == 'numerical':
                minimized = self.optimize_continuous_problem(optimizer, start_vals)
            elif self.optimization_type == 'categorical':
                minimized = self.optimize_categorical_problem(optimizer, start_vals)
            else:
                minimized = self.optimize_mixed_problem(optimizer, start_vals)

            self.success = minimized['success']
            if minimized['success']:
                new_params = {}
                for hp,v in zip(self.hyperparams, minimized['x']):
                    if hp.param_type == 'integer':
                        new_params[hp.name] = int(round(v))
                    elif hp.param_type == 'categorical':
                        new_params[hp.name] = str(v)
                    else:
                        new_params[hp.name] = v
                return new_params                
        else:
            self.success = False
            warnings.warn('optimizer did not converge! Continuing with randomly sampled data...')
            self.non_convergence_count += 1
            return {hp.name:v for hp,v in zip(self.hyperparams, start_vals[0])}

    def _param_dict_to_arr(self, param_dict):
        """
        Convert an unordered dictionary of parameter values to an ordered 
        list with the same order of parameters as in self.hyperparams

        Args:
            param_dict: a dictionary of parameters with parameter names as keys 
                    and parameter values as values

        Returns:
            a list of parameters with the same order as self.hyperparams
        """
        return [param_dict[hp.name] for hp in self.hyperparams]

    def _param_arr_to_dict(self, param_arr):
        """
        Convert an unordered dictionary of parameter values to an ordered 
        list with the same order of parameters as in self.hyperparams

        Args:
            params: a dictionary of parameters with parameter names as keys 
                    and parameter values as values

        Returns:
            a list of parameters with the same order as self.hyperparams
        """
        return {hp.name: p for hp, p in zip(self.hyperparams, param_arr)}

    def _fit_and_score_model(self, params, X_train, y_train, X_test, y_test, n_folds):
        if n_folds is not None:
            splits = self.get_kfold_split(n_folds, X_train)
            scores = []
            for train_idxs, test_idxs in splits:
                if self.model_module == 'xgboost':
                    dtrain = self.convert_to_xgboost_dataset(X_train[train_idxs], y_train[train_idxs])
                    dtest = self.convert_to_xgboost_dataset(X_train[test_idxs], y_train[test_idxs])
                    fitted_model = self.model.train(params, dtrain, evals=[(dtest, 'test')],
                        num_boost_round=params['n_estimators'], verbose_eval=False)
                    y_pred = fitted_model.predict(dtest)
                else:
                    new_model = self.build_new_model(params)
                    new_model.fit(X_train[train_idxs], y_train[train_idxs])
                    y_pred = new_model.predict(X_train[test_idxs])
                scores.append(self.eval_func(y_train[test_idxs], y_pred))
                score = np.mean(scores)
        else:
            if self.model_module == 'xgboost':
                dtrain = self.convert_to_xgboost_dataset(X_train, y_train)
                dtest = self.convert_to_xgboost_dataset(X_test, y_test)
                fitted_model = self.model.train(params, dtrain, evals=[(dtest, 'test')],
                        num_boost_round=params['n_estimators'], verbose_eval=False)
                y_pred = fitted_model.predict(dtest)
            else:
                new_model = self.build_new_model(params)
                new_model.fit(X_train, y_train)
                y_pred = new_model.predict(X_test)
            score = self.eval_func(y_test, y_pred)
        return score

    def _normalize_params(self, x):
        numeric_idxs = np.array([hp.param_type in ['integer', 'continuous'] for hp in self.hyperparams])
        means = np.array([(hp.upper - hp.lower)/2. for hp in self.hyperparams[numeric_idxs]])
        x[numeric_idxs, :] -= means
        # ranges can only be computed for numerical parameters
        ranges = []
        for hp, is_num in zip(self.hyperparams, numeric_idxs):
            if is_num:
                ranges.append(hp.upper - hp.lower)
            else:
                ranges.append(0)
        x[:,ranges>0] = x[:,ranges>0]/ranges[np.newaxis, ranges>0]
        return x

    def fit(self, X_train, y_train, X_test=None, y_test=None, n_iters=10, n_folds=None):
        """
        Given training data and optional validation data fit the machine learning model
        sequentially to find optimal hyperparameters. If X_test and y_test are provided 
        then the scoring/loss function is applied to the predictions on X_test 
        rather than X_train.

        Args:
            X_train: a numpy array with training data. each row corresponds to a data point
            y_train: a numpy array containing the target variable for the training data
            X_test: a numpy array with validation data. each row corresponds to a data point
            y_test: a numpy array containing the target variable for the validation data
            n_iters: number of iterations of bayesian optimization. default is 10.

        Returns:
            best_params: a dictionary with optimized hyperparameters
            best_model: an untrained model with the optimized hyperparameters 
        """
        if (X_test is None) and (y_test is None):
            X_test = X_train
            y_test = y_train
        elif (X_test is None) or (y_test is None):
            raise MissingValueException("Need to provide 'X_test' and 'y_test'")
        elif (X_test is not None) and (y_test is not None) and (n_folds is not None):
            raise Exception("Provide either 'X_test' and 'y_test' or 'n_folds'")

        self.non_convergence_count = 0
        optimizer = GaussianProcessRegressorWithCategorical(kernel=self.kernel,
                                                alpha=1e-4,
                                                n_restarts_optimizer=self.n_restarts_optimizer,
                                                normalize_y=True)
        for i in range(n_iters):            
            if i>0:           
                xs = [self._param_dict_to_arr(params) for score, params in self.hyperparam_history]
                xs = np.array(xs, dtype=object)
                if self.normalize:
                    xs = self._normalize_params(xs)
                ys = np.array([score for score, params in self.hyperparam_history])
                optimizer.fit(xs,ys)
                new_hyperparams = self.get_next_hyperparameters(optimizer)
            else:
                if self.start_vals is None:
                    new_hyperparams = self.get_random_values_dict()
                else:
                    new_hyperparams = self.get_default_values_dict()
            score = self._fit_and_score_model(new_hyperparams, X_train, y_train, X_test, y_test, 
                                              n_folds)

            self.hyperparam_history.append((score, new_hyperparams))
        
        best_params, best_model = self.get_best_params_and_model()
        return best_params, best_model
