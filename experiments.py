import os
import time
import pandas as pd
import numpy as np
from matplotlib import pyplot as plt
from scipy.stats import ttest_rel as ttest
from sklearn.manifold import MDS
from sim_normvec import VectorSimulator, euclidist
from sim_parser import ParserSimulator, ParsableStr, evalb
from sim_ranker import RankerSimulator, kendaltauscore
from eval_functions import oks_score_multi
from sim_binary import BinarySimulator, binary_match
from sim_keypoints import KeypointSimulator
from sim_segmentation import SegmentationSimulator, bb_intersection_over_union
from simulation import BetaDist
import utils
# import heuristic

def params_model(sim, opt, stan_data, skip_gold=False):
    ''' visually compare MAS parameters against known simulator parameters '''
    start = stan_data["n_gold_users"] if skip_gold else 0
    scatter_corr(opt["uerr"][start:], sim.err_rates[start:], title="User baseline")
    scatter_corr(opt.get("diff"), list(sim.difficulty_dict.values()), title="Item baseline")

def user_avg_dist(stan_data, apply_empirical_prior=True, nusers=None, missing_penalty_pct=0.1, simple=False):
    ''' BAU scores for each user = user's average distance across whole dataset '''
    sddf = pd.DataFrame(stan_data)
    s1 = sddf.groupby("u1s").sum()["distances"]
    s2 = sddf.groupby("u2s").sum()["distances"]
    n1 = sddf.groupby("u1s").count()["distances"]
    n2 = sddf.groupby("u2s").count()["distances"]
    count = n1.add(n2, fill_value=0)
    avg_distances = s1.add(s2, fill_value=0) / count
    if simple:
        return avg_distances
    if nusers is None:
        nusers = stan_data["NUSERS"]
    # if not np.all(avg_distances.index == count.index):
    index = range(1, nusers.values[0] if hasattr(nusers, "values") else nusers + 1)
    avg_distances = avg_distances.reindex(index)
    count = count.reindex(index).fillna(0)
    avg_distances = avg_distances.fillna(avg_distances.mean() * np.exp(missing_penalty_pct))
    if apply_empirical_prior:
        prior_mu, prior_var = avg_distances.mean(), avg_distances.var()
        sddf["u1delta"] = np.square(sddf["distances"] - avg_distances[sddf["u1s"]].values)
        sddf["u2delta"] = np.square(sddf["distances"] - avg_distances[sddf["u2s"]].values)
        v1 = sddf.groupby("u1s").sum()["u1delta"]
        v2 = sddf.groupby("u2s").sum()["u2delta"]
        var_distances = v1.add(v2, fill_value=0) / count
        var_distances = var_distances.fillna(0.01)
        var_distances += 0.1 * prior_var # to avoid division by zero variance
        nominator = prior_mu / prior_var + count * avg_distances / var_distances
        denominator = 1 / prior_var + count / var_distances
        avg_distances = nominator / denominator
    assert not np.isnan(avg_distances.values.sum())
    return avg_distances

def user_nearest_gold(stan_data):
    sddf = pd.DataFrame(stan_data)
    scores = sddf[sddf["u1s"]==1].groupby("u2s").mean()["distances"]
    score_mu = scores[scores > scores.min()].mean()
    return scores.reindex(range(1, stan_data["NUSERS"]+1), fill_value=score_mu)

def item_avg_dist(stan_data):
    ''' items' average distance '''
    sddf = pd.DataFrame(stan_data)
    avg_distances = sddf.groupby("items").mean()["distances"]
    return avg_distances

def correlate_global_local_uerr(stan_data, debug=False):
    uad = user_avg_dist(stan_data, simple=True)
    sddf = pd.DataFrame(stan_data)
    sddf["items"] = sddf["items"] - 1 # stan to python
    sddf["u1s"] = sddf["u1s"] - 1 # stan to python
    sddf["u2s"] = sddf["u2s"] - 1 # stan to python
    sddf["distances"] = sddf["distances"] - .1
    # does worker avg distance FOR THIS ITEM correlate with GLOBAL worker avg distance?
    corrs = []
    for item, idf in sddf.groupby("items"):
        iuad = user_avg_dist(idf, simple=True)
        uad_v, iuad_v = pd.concat([uad, iuad], axis=1, join="inner").values.T
        corr = np.corrcoef(iuad_v, uad_v)[0,1]
        corrs.append(corr)
        if debug:
            plt.scatter(iuad_v, uad_v)
        #     plt.title(kpexp.golddict.get(item))
            plt.xlim(0, 1.2)
            plt.ylim(0, 1.2)
            plt.xlabel("local worker avg distance")
            plt.xlabel("global worker avg distance")
            plt.show()
    return corrs
        

def scatter_corr(pred_vals, true_vals, jitter=False, title=None, log=False):
    ''' nice scatter plot '''
    if len(pred_vals) == 0:
        return
    if len(pred_vals) < len(true_vals):
        true_vals = np.array(true_vals)[pred_vals.index - 1]
    noise = lambda: np.random.uniform(-.5, .5, len(pred_vals)) if jitter else 0
    if len(pred_vals) < 1000:
        plt.scatter(np.array(pred_vals) + noise(), np.array(true_vals) + noise())
    else:
        plt.scatter(np.array(pred_vals) + noise(), np.array(true_vals) + noise(), s=1)
    if title is not None:
        plt.title(title) 
    if log:
        plt.xscale("log")
        plt.yscale("log")
    plt.show()
    print(np.corrcoef(pred_vals, true_vals))

def diagnostics(opt, stan_data):
    ''' print useful scatterplots for diagnosing MAS trained model results '''
    plt.rcParams.update({'font.size': 20})

    if "pred_distances" in opt:
        plt.scatter(opt["pred_distances"], stan_data["distances"])
        plt.xlabel("pred_distances")
        plt.ylabel("distances")
        plt.show()

    if "uerr" in opt:
        uerr_b = user_avg_dist(stan_data)
        plt.scatter(opt["uerr"], uerr_b)
        plt.xlabel("inferred $\gamma$")
        plt.ylabel("user average distance")
        plt.ylim()
        plt.show()

    if "diff" in opt:
        diff = item_avg_dist(stan_data)
        diffkeys = diff.index.values - 1
        plt.scatter(opt["diff"][diffkeys], diff)
        plt.xlabel("inferred diff")
        plt.ylabel("avg item distance")
        plt.show()

def pred_item(df, pred_uerr, label_colname, item_colname, uid_colname="uid"):
    ''' pick annotation per item according to lowest predicted user error '''
    df["pred_uerr"] = pred_uerr[df[uid_colname].values]
    def pickbybestuser(data):
        best_i = np.argmin(data["pred_uerr"].values)
        return data[label_colname].values[best_i]
    return df.groupby(item_colname).apply(pickbybestuser)

def get_model_user_rankings(opt, debug=False):
    ''' MAS model's user annotation ranking by item '''
    errs = opt["dist_from_truth"].copy()
    result = np.argsort(errs, axis=1)
    tmp = errs[0][result[0]]
    assert tmp[0] <= tmp[1]
    if debug:
        plt.figure(figsize=(8,4))
        errs[errs==666] = np.max(errs[errs<666]) * 1.1 # reset high-error value for better viz
        plt.imshow(errs.T, cmap='coolwarm', interpolation='nearest')
        plt.xlabel("items")
        plt.ylabel("users")
        plt.show()
    return result

def get_baseline_random(annodf, label_colname, item_colname):
    ''' random annotation per item '''
    def pickrandomlabel(data):
        return data.sample(1)[label_colname].values[0]
    return dict(annodf.groupby(item_colname).apply(pickrandomlabel))

def get_baseline_global_best_user(stan_data, annodf, label_colname, item_colname, uid_colname="uid", use_empirical_prior=True):
    ''' BAU scores per item: annotation by user with smallest average global distance '''
    uerrs = user_avg_dist(stan_data, apply_empirical_prior=use_empirical_prior).values
    return dict(pred_item(annodf, uerrs, label_colname, item_colname, uid_colname))

def get_baseline_honeypot_best_user(stan_data, annodf, label_colname, item_colname, uid_colname="uid"):
    uerrs = user_nearest_gold(stan_data).values
    return dict(pred_item(annodf, uerrs, label_colname, item_colname, uid_colname))

def get_baseline_item_centrallest(stan_data, annodf, label_colname, item_colname, uid_colname="uid",
                                    agg_fn=None):
    ''' SAD scores per item: annotation for item with smallest distance to other annotations of that item '''
    sddf = pd.DataFrame(stan_data)
    preds = {}
    for i0 in sorted(annodf[item_colname].unique()):
        i = i0 + 1
        iddf = sddf[sddf["items"] == i]
        if iddf.empty: # no pair distance just one label
            idf = annodf[annodf[item_colname] == i0]
            pred = idf[label_colname].values[0]
        else:
            uerrs = user_avg_dist(iddf, apply_empirical_prior=False)
            i_annodf = annodf[annodf[item_colname] == i-1]
            if agg_fn is None:
                best_user = uerrs.idxmin()
                pred = i_annodf[i_annodf[uid_colname] == best_user-1][label_colname].values[0]
            else:
                pred = agg_fn(i_annodf, uerrs)
        preds[i-1] = pred
    return preds

def get_oracle_preds(stan_data, annodf, label_colname, item_colname, uid_colname="uid",
                    eval_fn=None, gold_dict={}):
    ''' choose annotation closest to gold for each item according to evaluation function '''
    if eval_fn is None:
        raise ValueError("Need a evaluation function to compute oracle")
    def agg_fn(i_annodf, uerrs):
        item = i_annodf[item_colname].values[0]
        gold = gold_dict.get(item)
        if gold is None:
            return i_annodf[label_colname].values[0]
        evals = [eval_fn(gold, label) for label in i_annodf[label_colname].values]
        pred = i_annodf[label_colname].values[np.argmax(evals)]
        return pred
    return get_baseline_item_centrallest(stan_data, annodf, label_colname, item_colname, uid_colname, agg_fn)

def get_preds(annodf, per_item_user_rankings, label_colname, item_colname, user_colname="uid"):
    ''' MAS scores per item: annotation per item according to ranked annotations per item '''
    preds = {}
    for item in sorted(annodf[item_colname].unique()):
        idf = annodf[annodf[item_colname] == item]
        pred = None
        if len(idf) == 1:
            preds[item] = idf[label_colname].values[0]
            continue
        for best_user in per_item_user_rankings[item]:
            uidf = idf[idf[user_colname]==best_user]
            if len(uidf) > 0:
                pred = uidf[label_colname].values[0]
                break
        preds[item] = pred
    return preds

def eval_scores_vs(baseline_scores, model_scores, badness_threshold):
    ''' print and display comparison two sets of scores against each other '''
    diffs = np.array(model_scores) - np.array(baseline_scores)
    print(np.mean(baseline_scores), np.mean(model_scores))
    print("t-test", ttest(baseline_scores, model_scores))
    print("z-score", np.mean(diffs) / np.std(diffs))
    maxx = np.max(np.abs(diffs))
    print("baseline below thresh", (np.array(baseline_scores) < badness_threshold).mean())
    print("model below thresh", (np.array(model_scores) < badness_threshold).mean())
    # plt.hist(diffs, bins=np.linspace(-maxx, maxx, 10))
    # plt.show()

def eval_preds(preds, golds, eval_fn):
    ''' evaluate chosen annotations per item against known gold according to evaluation function '''
    scores = []
    for i, gold in sorted(golds.items(), key=lambda x:x[0]):
        score = eval_fn(gold, preds[i]) if preds.get(i) is not None else 0
        scores.append(score)
    return scores

def eval_preds_vs(baseline_preds, model_preds, golds, eval_fn, print_diffs=False, badness_threshold=0):
    ''' score two sets of chosen annotations against known gold according to evaluation function '''
    baseline_scores = eval_preds(baseline_preds, golds, eval_fn)
    model_scores = eval_preds(model_preds, golds, eval_fn)
    eval_scores_vs(baseline_scores, model_scores, badness_threshold)
    return baseline_scores, model_scores

def userset(data):
    result = set(data["u1s"]).union(set(data["u2s"]))
    return result

def _prune_fn(data, ratio):
    n_labels = len(data)
    n_remaining_labels = max(2, int(np.round(n_labels * (1 - ratio))))
    i = np.random.choice(len(data), n_remaining_labels, replace=False)
    return data.iloc[i]

class Experiment():
    ''' Contains raw annotations, processed data, model parameters, predictions, and known gold '''

    def __init__(self, label_colname, item_colname, uid_colname="uid"):
        self.label_colname = label_colname
        self.item_colname = item_colname
        self.uid_colname = uid_colname
        self.stan_data = None
        self.dem_opt = None
        self.mas_opt = None
        self.simulator = None
        self.annodf = None
        self.golddict = {}
        self.scoreboard = {}
        self.scoreboard_scores = {}
        self.badness_threshold = 0
        self.prune_ratio = 0
        self.extra_baseline_labels = {}
        self.supervised_items = {}
        self.supervised_labels = []
    
    def datacopy(self):
        exp_copy = type(self)(label_colname=self.label_colname, item_colname=self.item_colname, uid_colname=self.uid_colname)
        exp_copy.annodf = self.annodf
        exp_copy.stan_data = self.stan_data
        exp_copy.golddict = self.golddict
        exp_copy.supervised_items = self.supervised_items
        exp_copy.supervised_labels = self.supervised_labels
        return exp_copy

    def produce_stan_data(self, bound=True):
        ''' use distance function to create distance matrices and other data in its final form before training '''
        self.stan_data = utils.calc_distances(self.annodf, self.distance_fn, label_colname=self.label_colname, item_colname=self.item_colname, uid_colname=self.uid_colname, bound=bound)

    def preds_from_opt(self, opt):
        per_item_user_rankings = get_model_user_rankings(opt, debug=False)
        return get_preds(self.annodf, per_item_user_rankings, self.label_colname, self.item_colname, self.uid_colname)

    def setup_masX_data(self):
        seed = 1
        n_dims = self.stan_data["DIM_SIZE"]
        mds = MDS(n_components=n_dims, max_iter=500, eps=1e-9, random_state=seed, dissimilarity="precomputed", n_jobs=1)
        sddf = pd.DataFrame(self.stan_data)
        item_users = np.zeros((self.stan_data["NITEMS"], self.stan_data["NUSERS"]), dtype=int)
        embeddings = np.zeros((self.stan_data["NITEMS"], self.stan_data["NUSERS"], self.stan_data["DIM_SIZE"]))
        for item_plus1, idf in sddf.groupby("items"):
            uids = sorted(set(idf["u1s"]).union(set(idf["u2s"])))
            distM = np.zeros((len(uids), len(uids)))
            for _, ldf in idf.iterrows():
                distM[uids.index(ldf["u1s"]), uids.index(ldf["u2s"])] = ldf["distances"]
                distM[uids.index(ldf["u2s"]), uids.index(ldf["u1s"])] = ldf["distances"]
            embs = mds.fit_transform(distM)
            for user_i, emb in enumerate(embs):
                item_users[item_plus1-1, user_i] = uids[user_i]
                embeddings[item_plus1-1, user_i] = emb
        embeddings = embeddings - embeddings.mean(axis=1)[:,np.newaxis,:]
        return {"item_users":item_users, "embeddings":embeddings}

    def train(self, use_uerr=1, use_diff=1, norm_ratio=1, dim_size=3, dem_iter=500, mas_iter=500, masX_iter=500, num_samples=5, **kwargs):
        ''' trains and predicts using MAS, BAU, and SAD methods '''
        if self.stan_data is None:
            raise ValueError("Must setup stan_data first")

        dem_model = utils.stanmodel("dem2_semisup" if self.stan_data["NUSERS"] > 300 else "dem_semisup", overwrite=False)
        # dem_model = utils.stanmodel("dem2", overwrite=False)

        mas_model = utils.stanmodel("mas2_semisup", overwrite=False)

        self.stan_data["use_uerr"] = use_uerr
        self.stan_data["use_diff"] = use_diff
        self.stan_data["use_invlogit"] = 0
        self.stan_data["use_norm"] = 1
        self.stan_data["norm_ratio"] = norm_ratio
        # dim_size = int(self.annodf.groupby(self.item_colname).count()[self.label_colname].values.mean() * 4 / 5)
        self.stan_data["DIM_SIZE"] = dim_size
        self.stan_data["eps_limit"] = 3
        self.stan_data["uerr_prior_scale"] = 0.25
        self.stan_data["diff_prior_scale"] = 0.025
        self.stan_data["uerr_prior_loc_scale"] = 8
        self.stan_data["diff_prior_loc_scale"] = 8
        self.stan_data["alpha_loc"] = 0.0
        self.stan_data["alpha_scale"] = 0.5
        self.stan_data["beta_loc"] = 1.0
        self.stan_data["beta_scale"] = 0.01
        self.stan_data["sigma_scale"] = 1#0.1
        self.stan_data["err_scale"] = 0.1
        uerr_b = user_avg_dist(self.stan_data).values
        init = {
            "uerr_Z": uerr_b - np.mean(uerr_b),
            "uerr": uerr_b,
            "diff": np.ones(self.stan_data["NITEMS"])
        } if self.stan_data["use_uerr"] else {}

        self.stan_data = {**self.stan_data, **kwargs}

        self.bau_preds = get_baseline_global_best_user(self.stan_data, self.annodf, self.label_colname, self.item_colname, self.uid_colname)
        if self.stan_data["n_gold_users"] > 0:
            self.hon_preds = get_baseline_honeypot_best_user(self.stan_data, self.annodf, self.label_colname, self.item_colname, self.uid_colname)
        else:
            self.hon_preds = None
        self.sad_preds = get_baseline_item_centrallest(self.stan_data, self.annodf, self.label_colname, self.item_colname, self.uid_colname)

        # self.heu_scoreall = heuristic.score_all(self.stan_data)
        # per_item_user_rankings_heu = heuristic.per_item_user_rankings(self.heu_scoreall)
        # self.heu_preds = get_preds(self.annodf, per_item_user_rankings_heu, self.label_colname, self.item_colname, self.uid_colname)
        stan_opt_data = self.stan_data.copy()
        if stan_opt_data["n_gold_users"] > 0:
            stan_opt_data["gold_uerr"] = user_nearest_gold(self.stan_data)
        else:
            stan_opt_data["gold_uerr"] = np.zeros(stan_opt_data["NUSERS"])
        dem_start = time.time()
        self.dem_opt = dem_model.optimizing(data=stan_opt_data, init=init, verbose=True, iter=dem_iter)
        dem_end = time.time()
        mas_start = time.time()
        self.mas_opt = mas_model.optimizing(data=stan_opt_data, init=init, verbose=True, iter=mas_iter)
        mas_end = time.time()
        # if True or kwargs.get("timer"):
        #     print("dem", dem_end - dem_start)
        #     print("mas", mas_end - mas_start)

        self.dem_preds = self.preds_from_opt(self.dem_opt) if dem_iter > 0 else None
        self.mas_preds = self.preds_from_opt(self.mas_opt) if mas_iter > 0 else None
        self.masX_preds = None

        if masX_iter > 0:
            stan_opt_data.update(self.setup_masX_data())
            stan_opt_data["uerr_prior_scale"] = 0.25
            stan_opt_data["uerr_prior_loc_scale"] = 1
            init["uerr_center"] = stan_opt_data["uerr_prior_scale"] / 5
            init["center"] = stan_opt_data["embeddings"].mean(axis=1)
            init["diff"] = self.stan_data["diff_prior_scale"] * np.ones(self.stan_data["NITEMS"])
            masX_model = utils.stanmodel("masX", overwrite=False)
            # self.masX_opt = masX_model.optimizing(data=stan_opt_data, init=init, verbose=True, iter=masX_iter)
            self.masXsampling = masX_model.sampling(stan_opt_data, iter=500, chains=1)
            samples = self.masXsampling.extract()
            self.masX_opt = {k:v.mean(axis=0) for k, v in samples.items()}
            self.masX_preds = self.preds_from_opt(self.masX_opt)

        if hasattr(self, "train_DS"):
            self.train_DS()

        self.rand_preds = []
        for i in range(num_samples):
            self.rand_preds.append(get_baseline_random(self.annodf, self.label_colname, self.item_colname))
        self.oracle_preds = get_oracle_preds(self.stan_data, self.annodf, self.label_colname, self.item_colname, self.uid_colname, self.eval_fn, self.golddict)

    def eval_model(self, random_scores, model_preds, modelname, num_samples, verbose=True):
        ''' display comparison of model predictions vs baseline '''
        model_scores = eval_preds(model_preds, self.golddict, self.eval_fn)
        model_scores *= num_samples
        self.scoreboard["RANDOM USER"] = np.mean(random_scores)
        self.scoreboard_scores["RANDOM USER"] = random_scores
        self.scoreboard[modelname] = np.mean(model_scores)
        self.scoreboard_scores[modelname] = model_scores
        if verbose:
            print(modelname)
            eval_scores_vs(random_scores, model_scores, self.badness_threshold)
    
    def register_baseline(self, name, label_dict):
        ''' add additional baseline predictions if available '''
        self.extra_baseline_labels[name] = label_dict

    def test(self, num_samples=5, debug=False, **kwargs):
        ''' use known gold to test trained models and compare against each other and baselines '''
        # if self.simulator is not None:
        #     self.annodf = self.simulator.sim_df
        if self.annodf is None:
            raise ValueError("Must set annodf or create simulator")
        if self.mas_opt is None:
            raise ValueError("Must train model first")
        random_scores = []
        for random_preds in self.rand_preds:
            random_scores += eval_preds(random_preds, self.golddict, self.eval_fn)
        self.eval_model(random_scores, self.bau_preds, "BEST AVAILABLE USER", num_samples, verbose=debug)
        if self.hon_preds is not None:
            self.eval_model(random_scores, self.hon_preds, "BEST HONEYPOT USER", num_samples, verbose=debug)
        self.eval_model(random_scores, self.sad_preds, "SMALLEST AVERAGE DISTANCE", num_samples, verbose=debug)
        # self.eval_model(random_scores, self.heu_preds, "HEURISTIC", num_samples, verbose=debug)
        if hasattr(self, "ds_preds"):
            self.eval_model(random_scores, self.ds_preds, "DAWID SKENE", num_samples, verbose=debug)
        if self.dem_preds is not None:
            self.eval_model(random_scores, self.dem_preds, "DISTANCE EXPECTATION MAXIMIZATION", num_samples, verbose=debug)
        if self.mas_preds is not None:
            self.eval_model(random_scores, self.mas_preds, "MULTIDIMENSIONAL ANNOTATION SCALING", num_samples, verbose=debug)
        if hasattr(self, "masX_preds"):
            self.eval_model(random_scores, self.masX_preds, "MAS X", num_samples, verbose=debug)
        if hasattr(self, "extra_baseline_labels"):
            for baseline_name, baseline_preds in self.extra_baseline_labels.items():
                self.eval_model(random_scores, baseline_preds, baseline_name, num_samples, verbose=debug)
        self.eval_model(random_scores, self.oracle_preds, "ORACLE", num_samples, verbose=debug)
        if debug:
            debug_model = kwargs.get("debug_model") or "mas"
            debug_opt = getattr(self, F"{debug_model.lower()}_opt")
            get_model_user_rankings(debug_opt, debug=True)
            if kwargs.get("diagnose_vs_sim") and self.simulator is not None:
                params_model(self.simulator, debug_opt, self.stan_data)
            else:
                diagnostics(debug_opt, self.stan_data)
    
    def calc_stat_sig(self):
        self.stat_sig = {}
        non_oracle_sb = {k:v for k, v in self.scoreboard.items() if "oracle" not in k.lower()}
        maxscore_method = max(non_oracle_sb, key=non_oracle_sb.get)
        maxscore_scores = self.scoreboard_scores.get(maxscore_method)
        for methodname, scores in self.scoreboard_scores.items():
            if "oracle" in methodname.lower():
                continue
            t_test = ttest(scores, maxscore_scores)
            tstat = t_test.statistic
            pval = t_test.pvalue
            self.stat_sig[methodname] = pval


    def statistical_significance(self, methodname):
        if not hasattr(self, "stat_sig"):
            self.calc_stat_sig()
        return self.stat_sig.get(methodname)

    def calc_distmodel_scores(self, dist2wgt_fn=lambda x: 1/x):
        '''
        get predicted scores for each distance model
        for "dem", output is based on probability p rather than distance d,
        the transform for probability is p = softmax(d), so to convert dem score
        into the same space as all others, distance must be measured as log(p)
        '''
        sddf = pd.DataFrame(self.stan_data)
        item_userset = sddf.groupby("items").apply(userset)
        methods = {"sad":None, "bau":None, "dem":None, "mas":None, "masX":None}
        for method in methods.keys():
            self.annodf[F"{method}_dist"] = np.nan
            self.annodf[F"{method}_wgt"] = 1 # only relevant for decomposition
        self.annodf[F"orc_wgt"] = 1 # only relevant for decomposition
        methods["bau"] = user_avg_dist(self.stan_data, apply_empirical_prior=True)
        for i in range(self.stan_data["NITEMS"]):
            users = item_userset.get(i+1)
            if users is None: # only relevant for decomposition
                continue
            dem_logprobs = np.log(self.dem_opt["label_probabilities"] + 0.01)
            methods["dem"] = pd.Series({k+1:v for k, v in enumerate(-dem_logprobs[i])})
            methods["mas"] = pd.Series({k+1:v for k, v in enumerate(self.mas_opt["dist_from_truth"][i])})
            if hasattr(self, "masX_opt"):
                methods["masX"] = pd.Series({k+1:v for k, v in enumerate(self.masX_opt["dist_from_truth"][i])})
            iu_distances = sddf[sddf["items"]==i+1][["u1s", "u2s", "distances"]]
            methods["sad"] = user_avg_dist(iu_distances, apply_empirical_prior=False, nusers=self.stan_data["NUSERS"])
            gold = self.golddict.get(i)
            for u in users:
                idx = (self.annodf[self.uid_colname]==u-1) & (self.annodf[self.item_colname]==i)
                for methodname, methodvalue in methods.items():
                    distance = methodvalue.loc[u]
                    self.annodf.loc[idx, F"{methodname}_dist"] = distance
                    self.annodf.loc[idx, F"{methodname}_wgt"] = dist2wgt_fn(distance)
                    label = self.annodf.loc[idx, self.label_colname].values[0]
                    oracle_wgt = self.eval_fn(gold, label) if gold is not None and label is not None else 0
                    self.annodf.loc[idx, F"orc_wgt"] = oracle_wgt

    def weighted_merge(self, merge_fn, weights_colname=None):
        def agg_merge_fn(data):
            values = data[self.label_colname].values
            weights = data[weights_colname].values if weights_colname is not None else np.ones_like(values)
            return merge_fn(values, weights)
        return self.annodf.groupby(self.item_colname).apply(agg_merge_fn)
    
    def test_uerr_wgt_vote(self):
        self.calc_distmodel_scores()
        uerr_df = pd.DataFrame({self.uid_colname:np.arange(len(self.mas_opt["uerr"])), "mas_uerr":self.mas_opt["uerr"]})
        self.annodf = self.annodf.merge(uerr_df, on=self.uid_colname)
        for _, gdf in self.annodf.groupby(self.item_colname):
            mas_sad_diff = gdf["mas_dist"] - gdf["sad_dist"]
            plt.scatter(gdf["mas_uerr"], mas_sad_diff)
            plt.xlabel("inferred annotator error")
            plt.ylabel("MAS dist - SAD dist")
            plt.show()
        
    def compare_to_gold_dist(self):
        # SAD cant really do this for single-label items
        
        return

    def compare_mas_sad(self):
        sddf = pd.DataFrame(self.stan_data)
        item_userset = sddf.groupby("items").apply(userset)



    def debug(self, plot_stress=False, plot_vs_sad=False, plot_vs_gold=False, skip_miniplots=False, do_proper_scoring=True, use_PCA=True, print_labels=False):
        ''' tool for diving into results '''
        from sklearn.decomposition import PCA

        sddf = pd.DataFrame(self.stan_data)
        item_userset = sddf.groupby("items").apply(userset)
        bau = user_avg_dist(self.stan_data)
        all_scores = {}

        for i, iue in enumerate(self.mas_opt["item_user_errors"]):
            # if self.oracle_preds.get(i) != self.mas_preds.get(i):# or self.sad_preds.get(i) == self.mas_preds.get(i):
            #     continue
            gold = self.golddict.get(i)
            if gold is None:
                continue
            users = item_userset.get(i+1)
            usersX = users.copy()
            if "center" in self.mas_opt:
                users = sorted(users)
                usersX = list(np.arange(np.sum(np.abs(iue.mean(axis=1)) >= 0.0001)) + 1)
            if users is None or len(users) < 2:
                continue
            dist_from_truth = self.mas_opt["dist_from_truth"][i]
            iu_distances = sddf[sddf["items"]==i+1][["u1s", "u2s", "distances"]]
            if not skip_miniplots:
                print("item", str(i+1))
                # print(iu_distances)
            sad = user_avg_dist(iu_distances, apply_empirical_prior=False, nusers=self.stan_data["NUSERS"])
            sad_scores = [sad.loc[u] for u in users]
            bau_scores = [bau.loc[u] for u in users]
            # if len(iue[0]) > 2:
            #     all_embeddings = PCA(n_components=2).fit_transform(iue)
            # embeddings = np.array([all_embeddings[u-1] for u in users])
            embeddings = np.array([iue[u-1] for u in usersX])
            if use_PCA and len(embeddings[0]) > 2:
                embeddings = PCA(n_components=2).fit_transform(embeddings)
            else:
                embeddings = embeddings[:,:2]
            mas_scores = [dist_from_truth[u-1] for u in users]
            skills = [self.mas_opt["uerr"][u-1] for u in users]
            scale = np.max(np.abs(embeddings)) * 1.05
            
            # plot preds
            idf = self.annodf[self.annodf[self.item_colname]==i]

            if plot_vs_gold:
                labels = [idf[idf[self.uid_colname] == u-1][self.label_colname].values[0] for u in users]
                
                if gold is not None:
                    gold_scores = [self.eval_fn(gold, label) for label in labels]
                else:
                    continue
                    gold_scores = np.nan * np.zeros(len(labels))
                if not skip_miniplots:
                    # diff = self.mas_opt["diff"][i]
                    plt.scatter(mas_scores, gold_scores)
                    plt.scatter(sad_scores, gold_scores, color="red")
                    # plt.title(diff)
                    plt.show()
                if do_proper_scoring:
                    all_scores.setdefault("mas_properscore", []).append(utils.proper_score(mas_scores, gold_scores))
                    all_scores.setdefault("sad_properscore", []).append(utils.proper_score(sad_scores, gold_scores))
                    all_scores.setdefault("bau_properscore", []).append(utils.proper_score(bau_scores, gold_scores))
                try:
                    all_scores.setdefault("mas_scores", []).extend(mas_scores)
                    all_scores.setdefault("sad_scores", []).extend(sad_scores)
                    all_scores.setdefault("bau_scores", []).extend(bau_scores)
                    all_scores.setdefault("gold_scores", []).extend(gold_scores)
                except:
                    pass

            # plot stress
            if plot_stress:
                for _, row in iu_distances.iterrows():
                    u1, u2 = (int(row["u1s"]-1), int(row["u2s"]-1))
                    u1emb = embeddings[np.where(u1+1==np.array(list(usersX)))]
                    u2emb = embeddings[np.where(u2+1==np.array(list(usersX)))]
                    embs = np.concatenate((u1emb, u2emb)).T
                    stress = row["distances"] - np.linalg.norm(iue[u1] - iue[u2])
                    cmap = plt.cm.Reds if stress > 0 else plt.cm.Greens
                    plt.plot(embs[0], embs[1], color=cmap(10*stress))

            all_scores.setdefault("BAU chosen", []).append(self.eval_fn(gold, self.bau_preds.get(i)))
            all_scores.setdefault("SAD chosen", []).append(self.eval_fn(gold, self.sad_preds.get(i)))
            all_scores.setdefault("MAS chosen", []).append(self.eval_fn(gold, self.mas_preds.get(i)))
            all_scores.setdefault("ORC chosen", []).append(self.eval_fn(gold, self.oracle_preds.get(i)))
            if not skip_miniplots:
                def plot_pred(preds, marker, color, size=50):
                    this_pred = preds.get(i)
                    this_uid = idf[[np.array_equal(v, this_pred) for v in idf[self.label_colname]]][self.uid_colname].values[0]
                    this_ui = np.where(np.array([u-1 for u in users]) == this_uid)[0][0]
                    this_emb = embeddings[this_ui]
                    plt.scatter([this_emb[0]], [this_emb[1]], marker=marker, c=color, s=size)
                plot_pred(self.oracle_preds, "o", "gold", 100)
                # plot_pred(self.bau_preds, "+", "black")
                plot_pred(self.sad_preds, "+", "blue")
                plot_pred(self.mas_preds, "x", "magenta")
                mean_SAD = np.mean(embeddings, axis=0)
                # plt.scatter([mean_SAD[0]], [mean_SAD[1]], marker="o", c="k", s=50)
                plt.scatter([mean_SAD[0]], [mean_SAD[1]], marker=0, c="b", s=10)
                # plt.legend(["Oracle", "SAD chosen", "MAS chosen"], loc="upper left")
                # plt.scatter(embeddings[:,0], embeddings[:,1])
                labels = [idf[idf[self.uid_colname] == u-1][self.label_colname].values[0] for u in users]
                center = (0, 0)
                if "center" in self.mas_opt:
                    if use_PCA and len(embeddings[0]) > 2:
                        raise ValueError("must set use_PCA=False if using center")
                    center = self.mas_opt["center"][i]
                plt.scatter(center[0], center[1], marker=0, c="m", s=10)
                print(center, "center")
                for ui, emb in enumerate(embeddings):
                    print(emb, skills[ui])
                    plt.plot([mean_SAD[0], emb[0]], [mean_SAD[1], emb[1]], "b:", alpha=0.25)
                    plt.plot([center[0], emb[0]], [center[1], emb[1]], "m:", alpha=0.5)
                    ha = "right"
                    if plot_vs_gold:
                        pltanno = str(np.round(gold_scores[ui],2))
                    else:
                        pltanno = F"{list(users)[ui]-1}\n$\gamma$={np.round(skills[ui],2)}"
                        # pltanno = str(list(users)[ui]) + ":" + str(np.round(mas_scores[ui],2)) + ":" + str(np.round(skills[ui],2))
                    if print_labels:
                        pltanno = labels[ui]
                        if emb[0] < -.2:
                            ha = "right"
                        elif emb[0] > .2:
                            ha = "left"
                        else:
                            ha = "center"
                    plt.annotate(pltanno, emb, ha=ha)

                plt.xlim(-scale, scale)
                plt.ylim(-scale, scale)
                plt.show()
                sad_select_eval = self.eval_fn(gold, self.sad_preds.get(i))
                mas_select_eval = self.eval_fn(gold, self.mas_preds.get(i))
                print(sad_select_eval, mas_select_eval)

            if plot_vs_sad:
                plt.scatter(mas_scores, sad_scores)
                plt.show()
                print("SAD vs MAS: ", self.sad_preds.get(i), self.mas_preds.get(i))
        
        if plot_vs_gold:

            plt.scatter(all_scores["mas_scores"], all_scores["gold_scores"])
            plt.scatter(all_scores["sad_scores"], all_scores["gold_scores"], color="r")
            plt.scatter(all_scores["bau_scores"], all_scores["gold_scores"], color="y")
            plt.legend(["mas", "sad", "bau"])
            plt.show()
            print("\n ALL")
            print("ru", 0, np.std(all_scores["gold_scores"]))
            print("bau", np.corrcoef(all_scores["bau_scores"], all_scores["gold_scores"])[0,1], np.std((1 - np.array(all_scores["bau_scores"])) - np.array(all_scores["gold_scores"])))
            print("sad", np.corrcoef(all_scores["sad_scores"], all_scores["gold_scores"])[0,1], np.std((1 - np.array(all_scores["sad_scores"])) - np.array(all_scores["gold_scores"])))
            print("mas", np.corrcoef(all_scores["mas_scores"], all_scores["gold_scores"])[0,1], np.std((1 - np.array(all_scores["mas_scores"])) - np.array(all_scores["gold_scores"])))

            fig, ax = plt.subplots()
            ax.scatter(1.1-np.array(all_scores["mas_scores"]), all_scores["gold_scores"])
            fs = 24
            # fig.suptitle('Score-all evaluation example', fontsize=fs)
            ax.set_xlabel("1 - MAS $\\varepsilon$ scores", fontsize=fs)
            ax.set_ylabel("Gold scores", fontsize=fs)
            ax.tick_params(axis='both', which='major', labelsize=fs)
            plt.show()

            print("Proper scores")
            print("MAS", np.mean(all_scores["mas_properscore"]))
            print("SAD", np.mean(all_scores["sad_properscore"]))
            print("BAU", np.mean(all_scores["bau_properscore"]))
            return all_scores

    def describe_data(self):
        ''' describes data, but must be called after producing stan_data '''
        nusers = self.stan_data["NUSERS"]
        nitems = self.stan_data["NITEMS"]
        nlabels = len(self.annodf)
        ulabels = self.annodf.groupby(self.uid_colname).count()[self.label_colname].values
        ilabels = self.annodf.groupby(self.item_colname).count()[self.label_colname].values
        lperu = str(np.mean(ulabels).round(2)) + "$\pm$" + str(2 * np.std(ulabels).round(2))
        lperi = str(np.mean(ilabels).round(2)) + "$\pm$" + str(2 * np.std(ilabels).round(2))

        self.annodf["labelstr"] = self.annodf[self.label_colname].astype(str)
        label_occurrences = self.annodf.groupby([self.item_colname, "labelstr"]).count()[self.label_colname].values
        dupes = np.sum(label_occurrences > 1)

        cols = [nusers, nitems, nlabels, lperu, lperi, dupes]
        print("nusers", "nitems", "nlabels", "lperu", "lperi", "dupes")
        print(" & ".join(map(str, cols)))
    
    def prune_data(self, ratio):
        return self.annodf.groupby(self.item_colname).apply(_prune_fn, ratio).reset_index(drop=self.item_colname)

    def remove_supervised(self, ngoldu):
        ''' ONLY FOR SIMULATOR EXPERIMENTS: remove semi-supervised items from test set '''
        if ngoldu > 0:
            goldi = self.annodf[self.annodf[self.uid_colname] < ngoldu][self.item_colname].unique()
            for i in goldi:
                del self.golddict[i]
    
    def get_recombined_preds(self, granular_preds_dict=None):
        if granular_preds_dict is None:
            return {}
        else:
            # gran_preds = np.array(list(granular_preds_dict.values()))
            gran_preds = granular_preds_dict
            def recombine(data):
                item_ids = data[self.item_colname].unique()
                item_gran_preds = utils.flatten([gran_preds[i] for i in item_ids])
                try:
                    item_gran_preds = sorted(item_gran_preds)
                finally:
                    return item_gran_preds
            recombined_preds = self.annodf.groupby(self.merge_index_colname).apply(recombine)
            return dict(recombined_preds)
    
    def backup_preds(self):
        if not hasattr(self, "backup_preds_dict"):
            self.backup_preds_dict = {
                "bau_preds": self.bau_preds,
                # "hon_preds": self.hon_preds,
                "sad_preds": self.sad_preds,
                # "heu_preds": self.heu_preds,
                "dem_preds": self.dem_preds,
                "mas_preds": self.mas_preds,
                "oracle_preds": self.oracle_preds,
                "gran_gold": self.golddict
            }
            for k, v in self.extra_baseline_labels.items():
                self.backup_preds_dict[k] = v
        else:
            for k, v in self.backup_preds_dict.items():
                setattr(self, k, v)
    
    def register_weighted_merge(self):
        if not hasattr(self, "merge_fn"):
            print("No merge_fn for test_weighted_merge")
            return
        self.calc_distmodel_scores()
        self.backup_preds()
        self.register_baseline("Uniform Merge", self.weighted_merge(self.merge_fn))
        self.register_baseline("BAU Merge", self.weighted_merge(self.merge_fn, "bau_wgt"))
        self.register_baseline("SAD Merge", self.weighted_merge(self.merge_fn, "sad_wgt"))
        self.register_baseline("DEM Merge", self.weighted_merge(self.merge_fn, "dem_wgt"))
        self.register_baseline("MAS Merge", self.weighted_merge(self.merge_fn, "mas_wgt"))
        # self.register_baseline("Oracle Merge", self.weighted_merge(self.merge_fn, "orc_wgt"))
    
    def test_recombination(self, orig_golddict, num_samples=5, debug=False, **kwargs):
        self.backup_preds()
        self.bau_preds = self.get_recombined_preds(granular_preds_dict=self.bau_preds)
        # self.hon_preds = self.get_recombined_preds(granular_preds_dict=self.hon_preds)
        self.sad_preds = self.get_recombined_preds(granular_preds_dict=self.sad_preds)
        # self.heu_preds = self.get_recombined_preds(granular_preds_dict=self.heu_preds)
        self.dem_preds = self.get_recombined_preds(granular_preds_dict=self.dem_preds)
        self.mas_preds = self.get_recombined_preds(granular_preds_dict=self.mas_preds)
        self.oracle_preds = self.get_recombined_preds(granular_preds_dict=self.oracle_preds)
        for i in range(len(self.rand_preds)):
            self.rand_preds[i] = self.get_recombined_preds(granular_preds_dict=self.rand_preds[i])
        new_extra_baseline_labels = {}
        for k, v in self.extra_baseline_labels.items():
            new_extra_baseline_labels[k] = self.get_recombined_preds(granular_preds_dict=dict(v))
        self.extra_baseline_labels = new_extra_baseline_labels
        self.golddict = orig_golddict
        self.test(num_samples=num_samples, debug=debug, **kwargs)
    

    def calc_krippendorf_alpha_ours(self):
        goldvals = list(self.golddict.values())
        goldpairs = utils.flatten([[(goldvals[i], goldvals[j]) for j in range(i+1, len(goldvals))] for i in range(len(goldvals))])
        intra_item_dists = [self.distance_fn(*gp) for gp in goldpairs]
        De = np.mean(intra_item_dists)
        dists = utils.calc_distances(self.annodf, self.distance_fn, label_colname=self.label_colname, item_colname=self.item_colname, uid_colname=self.uid_colname, bound=True)
        dist_df = pd.DataFrame(dists)
        Do = dist_df.groupby("items")["distances"].mean()
        Do_mean = np.mean(Do)
        return 1 - Do_mean / De

    def calc_krippendorf_alpha(self):
        return dorff(self.annodf,
                    experiment_col=self.item_colname,
                    annotator_col=self.uid_colname,
                    class_col=self.label_colname,
                    metric_fn=self.distance_fn)

def run_square(annodf, uid_colname, item_colname, label_colname, squaredir="square-2.0/"):
    ''' put data into SQUARE format for running things like ZenCrowd '''
    outdir = squaredir + "data/test/"
    cats = pd.Categorical(map(str, annodf[label_colname])).codes
    cat_lookup = dict(zip(cats, annodf[label_colname]))
    annodf["cat"] = cats
    catdf = pd.DataFrame(np.unique(cats))
    catdf.iloc[:-1].to_csv(outdir + "categories.txt", header=False, index=False, sep=" ")
    catdf.iloc[-1:].to_csv(outdir + "categories.txt", header=False, index=False, sep=" ", mode='a', line_terminator="")
    respdf = annodf[[uid_colname, item_colname, "cat"]]
    respdf.iloc[:-1].to_csv(outdir + "responses.txt", header=False, index=False, sep=" ")
    respdf.iloc[-1:].to_csv(outdir + "responses.txt", header=False, index=False, sep=" ", mode='a', line_terminator="")
        # --responses ./data/test/responses.txt --category ./data/test/categories.txt --method Zen --estimation unsupervised --saveDir ./inferredGold/test/
        

class ParserSingleton():
    def __init__(self, num_items=100, min_sentence_length=6, max_sentence_length=20):
        from nltk.data import find as nltkfind
        from nltk.parse.bllip import BllipParser
        print("Loading BLLIP")
        bllip_dir = nltkfind('models/bllip_wsj_no_aux').path
        self.BLLIP = BllipParser.from_unified_model_dir(bllip_dir)
        self.generate_sentences(num_items=num_items, min_sentence_length=min_sentence_length, max_sentence_length=max_sentence_length)
    def generate_sentences(self, num_items, min_sentence_length=6, max_sentence_length=20, seed=0):
        from nltk.corpus import brown as browncorpus
        print("Sampling Brown corpus sentences")
        np.random.seed(seed)
        sentences = np.random.choice(browncorpus.sents(), num_items * 3, replace=False)
        self.sentences = [s for s in sentences if len(s) >= min_sentence_length and len(s) <= max_sentence_length][:num_items]
        if len(self.sentences) < num_items:
            print(F"WARNING, only got {len(self.sentences)}/{num_items} sentences")

class ParserExperiment(Experiment):
    ''' experiment using simulated parse data '''
    def __init__(self, preloaded_singleton=None):
        super().__init__("parse", "sentenceId")
        if preloaded_singleton is None:
            self.singleton = ParserSingleton()
        else:
            self.singleton = preloaded_singleton
        self.BLLIP = self.singleton.BLLIP
        self.sentences = self.singleton.sentences
        self.eval_fn = evalb
        self.badness_threshold = 0.9
    
    def setup(self, num_items, n_users, pct_items, uerr_a=1, uerr_b=4, difficulty_a=1, difficulty_b=100,
                    ngoldu=0):
        if num_items > len(self.sentences):
            raise ValueError("Number of items to setup larger than sentences generated")
        self.simulator = ParserSimulator(self.BLLIP, self.sentences[:num_items])
        self.stan_data = self.simulator.create_stan_data_scenario(n_users=n_users, pct_items=pct_items,
                                                    uerr_a=uerr_a, uerr_b=uerr_b,
                                                    difficulty_a=difficulty_a, difficulty_b=difficulty_b,
                                                    n_gold_users=ngoldu)
        gold_parses = [self.BLLIP.parse_one(ParsableStr(s)) for s in self.simulator.df.tokens.values]
        self.golddict = dict(enumerate(gold_parses))
        self.annodf = self.simulator.sim_df
        self.remove_supervised(ngoldu)

class RankerExperiment(Experiment):
    ''' experiment using simulated rankings data '''
    def __init__(self, base_dir="data/qrels.all.txt"):
        super().__init__("rankings", "topic_item")
        self.base_dir = base_dir
        self.eval_fn = kendaltauscore
    def setup(self, n_items, n_users, pct_items, uerr_a, uerr_b, difficulty_a, difficulty_b, ngoldu=0):
        self.simulator = RankerSimulator(self.base_dir, n_items=n_items)
        self.simulator.eval_fn = self.eval_fn
        self.stan_data = self.simulator.create_stan_data_scenario(n_users=n_users, pct_items=pct_items,
                                                    uerr_a=uerr_a, uerr_b=uerr_b,
                                                    difficulty_a=difficulty_a, difficulty_b=difficulty_b,
                                                    n_gold_users=ngoldu)
        self.golddict = self.simulator.gold.to_dict()
        self.annodf = self.simulator.sim_df
        self.remove_supervised(ngoldu)
    def setup_standard(self):
        self.setup(n_items=100, n_users=20, pct_items=0.2, uerr_a=-1.0, uerr_b=0.8, difficulty_a=-2.0, difficulty_b=1.3, ngoldu=0)

class BinaryExperiment(Experiment):
    ''' experiment using simulated binary categorical data '''
    def __init__(self, p_true=0.5):
        super().__init__("label", "item")
        self.eval_fn = binary_match
        self.p_true = p_true
    def setup(self, n_items, n_users, pct_items, uerr_a, uerr_b, difficulty_a, difficulty_b, ngoldu=0):
        self.simulator = BinarySimulator(n_items=n_items, p_true=self.p_true)
        self.simulator.eval_fn = self.eval_fn
        self.stan_data = self.simulator.create_stan_data_scenario(n_users=n_users, pct_items=pct_items,
                                                    uerr_a=uerr_a, uerr_b=uerr_b,
                                                    difficulty_a=difficulty_a, difficulty_b=difficulty_b,
                                                    n_gold_users=ngoldu)
        self.golddict = self.simulator.df.gold.to_dict()
        self.annodf = self.simulator.sim_df
        self.remove_supervised(ngoldu)
    def setup_standard(self):
        self.setup(n_items=300, n_users=30, pct_items=0.2, uerr_a=1, uerr_b=1, difficulty_a=1, difficulty_b=100, ngoldu=0)
    def train_DS(self, algorithm='FDS', verbose=False):
        from fastds import run as run_fastds
        items = []
        responses = {}
        for item, idf in self.annodf.groupby(self.item_colname):
            items.append(item)
            label_dict = {}
            for uid, udf in idf.groupby(self.uid_colname):
                label_dict[uid] = list(udf[self.label_colname].values)
            responses[item] = label_dict
        preds = run_fastds(responses, algorithm, verbose)
        self.ds_preds = dict(zip(items, preds))
        

class KeypointsExperiment(Experiment):
    ''' experiment using simulated keypoint data '''
    def __init__(self):
        super().__init__("annotation", "item")
        self.eval_fn = oks_score_multi
    def setup(self, n_items, n_users, pct_items, uerr_a, uerr_b, difficulty_a=1, difficulty_b=1, ngoldu=0):
        self.simulator = KeypointSimulator(max_items=n_items)
        self.simulator.eval_fn = self.eval_fn
        self.stan_data = self.simulator.create_stan_data_scenario(n_users=n_users, pct_items=pct_items,
                                                    uerr_a=uerr_a, uerr_b=uerr_b,
                                                    difficulty_a=difficulty_a, difficulty_b=difficulty_b,
                                                    n_gold_users=ngoldu)
        self.golddict = self.simulator.df.gold.to_dict()
        self.annodf = self.simulator.sim_df
        self.remove_supervised(ngoldu)
    def setup_standard(self):
        self.setup(n_items=100, n_users=20, pct_items=0.2, uerr_a=-1.0, uerr_b=0.8, difficulty_a=-2.0, difficulty_b=1.3, ngoldu=0)

class SegmentationExperiment(Experiment):
    ''' experiment using simulated bounding box data '''
    def __init__(self, base_dir):
        super().__init__("label", "item")
        self.base_dir = base_dir
        self.eval_fn = bb_intersection_over_union
    def setup(self, n_items, n_users, pct_items, uerr_a, uerr_b, difficulty_a, difficulty_b, ngoldu=0):
        self.simulator = SegmentationSimulator(self.base_dir, max_items=n_items)
        self.stan_data = self.simulator.create_stan_data_scenario(n_users=n_users, pct_items=pct_items,
                                                    uerr_a=uerr_a, uerr_b=uerr_b,
                                                    difficulty_a=difficulty_a, difficulty_b=difficulty_b,
                                                    n_gold_users=ngoldu)
        self.golddict = self.simulator.gold.to_dict()
        self.annodf = self.simulator.sim_df
        self.remove_supervised(ngoldu)
    def setup_standard(self):
        self.setup(n_items=100, n_users=20, pct_items=0.2, uerr_a=-1.0, uerr_b=0.8, difficulty_a=-2.0, difficulty_b=1.3, ngoldu=0)


class VectorExperiment(Experiment):
    ''' TODO experiment using simulated vector data '''
    def __init__(self):
        super().__init__("label", "topic_item")
        self.eval_fn = lambda x, y: -euclidist(x, y)
    def setup(self, n_items, n_users, pct_items, uerr_a, uerr_b, difficulty_a, difficulty_b, ngoldu=0, n_dims=8):
        self.simulator = VectorSimulator(n_items, n_dims)
        self.stan_data = self.simulator.create_stan_data_scenario(n_users=n_users, pct_items=pct_items,
                                                    uerr_a=uerr_a, uerr_b=uerr_b, n_gold_users=ngoldu,
                                                    difficulty_a=difficulty_a, difficulty_b=difficulty_b)
        self.annodf = self.simulator.sim_df
        self.golddict = self.simulator.df.gold.to_dict()
def setup_vector_standard(vector_experiment):
    vector_experiment.setup(n_items=100, n_users=50, pct_items=0.2, uerr_a=-1.0, uerr_b=0.8,
                            difficulty_a=-2.0, difficulty_b=.3, ngoldu=0)

class RealExperiment(Experiment):
    ''' experiment using real data provided by requester '''
    def __init__(self, eval_fn=None, label_colname="label", item_colname="item", uid_colname="uid", distance_fn=None):
        super().__init__(label_colname, item_colname, uid_colname)
        self.eval_fn = eval_fn
        self.distance_fn = distance_fn if distance_fn is not None else (lambda x,y: 1 - self.eval_fn(x, y))
    def setup(self, annodf, golddf=None, c_anno_uid=None, c_anno_item=None, c_anno_label=None, c_gold_item=None, c_gold_label=None, merge_index=None):
        renamey = lambda y: self.label_colname if "label" in y else self.item_colname if "item" in y else self.uid_colname if "uid" in y else y
        localargs = locals()
        colrename = {localargs[k]:renamey(k) for k in localargs if "c_" in k and localargs[k] is not None}
        self.annodf = annodf[[c_anno_uid or self.uid_colname, c_anno_item or self.item_colname, c_anno_label or self.label_colname]]
        self.annodf = self.annodf.rename(columns=colrename)[[self.uid_colname, self.item_colname, self.label_colname]]
        if self.prune_ratio:
            self.annodf = self.prune_data(self.prune_ratio)
        if merge_index is not None:
            self.merge_index_colname = merge_index
            self.annodf[merge_index] = annodf[merge_index]
        self.annodf = self.annodf.dropna().copy()
        self.uiddict = utils.make_categorical(self.annodf, self.uid_colname)
        self.itemdict = utils.make_categorical(self.annodf, self.item_colname)
        if golddf is not None:
            golddf = golddf[[c_gold_item or self.item_colname, c_gold_label or self.label_colname]]
            golddf = golddf.rename(columns=colrename)[[self.item_colname, self.label_colname]]
            golddf = utils.translate_categorical(golddf, self.item_colname, self.itemdict)
            self.golddf = golddf
            self.golddict = golddf.set_index(self.item_colname).to_dict()[self.label_colname]
            self.golddict = {k: v for k, v in self.golddict.items() if v is not None}
        self.produce_stan_data()

class CategoricalExperiment(RealExperiment):
    ''' TODO experiment using real simple data '''

    def __init__(self, eval_fn=None, distance_fn=None):
        if eval_fn is None:
            eval_fn = lambda x, y: (1 if x == y else 0)
        super().__init__(eval_fn=eval_fn, distance_fn=distance_fn)


# semi-supervised learning
def remove_supervised_items(expermnt):
    expermnt.orig_golddict = expermnt.golddict.copy()
    for item in expermnt.supervised_items:
        try:
            del expermnt.golddict[item]
        except:
            pass

def rename_items(expermnt, items):
    return [expermnt.itemdict[x] for x in items]

def set_supervised_items_preset(expermnt, golditems, apply_fn=lambda x:x):
    ''' when you have pre-decided gold items for semisupervised learning,
    call this after calling setup but before training to remove semi-supervised items from training and test set'''
    expermnt.supervised_items = golditems
    expermnt.supervised_labels = [apply_fn(expermnt.golddict.get(item)) for item in expermnt.supervised_items]
    assert expermnt.golddict is not None
    remove_supervised_items(expermnt)

def set_supervised_items(expermnt, n_supervised_items, apply_fn=lambda x:x, randomize=False):
    ''' when you want to simulate honeypot questions by using the most-answered items as gold,
    call this after calling setup but before training to remove semi-supervised items from training and test set '''
    most_annotated_items = expermnt.annodf.groupby(expermnt.item_colname).count()[expermnt.label_colname].sort_values(ascending=False)
    most_annotated_indices = [i for i in most_annotated_items.index.values if i in expermnt.golddict]
    golditems = most_annotated_indices[:n_supervised_items]
    set_supervised_items_preset(expermnt, golditems, apply_fn)

def make_supervised_standata(expermnt, model_gold_err=-4):
    ''' adds semi-supervised items back to training set (not test set) and tells MAS they are known gold '''
    # if n_supervised_items == 0:
    #     expermnt.produce_stan_data()
    #     return
    assert expermnt.golddict is not None
    
    supervised_df = pd.DataFrame({
        expermnt.uid_colname:np.zeros(len(expermnt.supervised_items), dtype=int),
        expermnt.item_colname:expermnt.supervised_items,
        expermnt.label_colname:expermnt.supervised_labels
        })
    expermnt.annodf = expermnt.annodf.copy()
    expermnt.annodf[expermnt.uid_colname] += 1
    expermnt.annodf = pd.concat([supervised_df, expermnt.annodf], sort=True).sort_values(expermnt.uid_colname)
    expermnt.produce_stan_data()
    expermnt.stan_data["n_gold_users"] = 1
    expermnt.stan_data["gold_user_err"] = model_gold_err