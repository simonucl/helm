from abc import ABC
from dataclasses import dataclass, replace
from typing import List, Dict, Tuple, Optional, Any, Union, Set
from collections import defaultdict

from common.object_spec import ObjectSpec, create_object
from common.general import singleton
from .statistic import Stat, merge_stat
from .augmentations.perturbation_description import PerturbationDescription
from .adapter import (
    AdapterSpec,
    ScenarioState,
    RequestState,
    ADAPT_LANGUAGE_MODELING,
)
from .metric_name import MetricName
from .metric_service import MetricService
from .scenarios.scenario import Instance


@dataclass(unsafe_hash=True)
class PerInstanceStatsKey:
    """
    `PerInstanceStatsKey` is a (instance, trial index) tuple.
    """

    instance: str
    trial_index: int

    def __init__(self, instance: Instance, trial_index: int):
        self.instance = instance.id if instance.id is not None else str(instance)
        self.trial_index = trial_index


@dataclass
class MetricResult:
    """
    `MetricResult` is a wrapper around aggregated statistics (averaged over instances and trial index),
    and per-(instance, trial index) statistics.
    """

    aggregated_stats: List[Stat]

    # Key for per-instance statistics is (instance, trial index), value is list of statistics.
    per_instance_stats: Dict[PerInstanceStatsKey, List[Stat]]


class Metric(ABC):
    """
    A `Metric` takes the results of execution and produces `Stat`s for a
    scenario.

    Note: `Metric` actually right now is a bit of misnomer because it produces many
    `Stat`s, that might be distinct but are computed together.  Eventually we
    might move to a world where there is one (or very few metrics that are domain-independent).
    """

    def evaluate(
        self, scenario_state: ScenarioState, metric_service: MetricService, eval_cache_path: str
    ) -> MetricResult:
        """
        Main entry point for a `Metric`.  This function groups the single
        list of `RequestState` by training trial and instance, and invokes
        other functions to process those.  This should serve most purposes.

        Any logic that doesn't decompose along instances should go here, such
        as robustness.
        """
        if scenario_state.adapter_spec.method == ADAPT_LANGUAGE_MODELING:
            return self.evaluate_language_modeling(scenario_state, metric_service, eval_cache_path)

        adapter_spec = scenario_state.adapter_spec
        global_stats: Dict[MetricName, Stat] = {}  # MetricName -> Stat
        all_per_instance_stats: Dict[PerInstanceStatsKey, List[Stat]] = {}

        for train_trial_index in range(adapter_spec.num_train_trials):
            trial_stats: Dict[MetricName, Stat] = {}  # Statistics just for this trial
            per_instance_stats: Dict[Instance, List[Stat]] = defaultdict(list)  # Stats for individual instances

            for instance in scenario_state.instances:
                instance_stats = []

                # Evaluate generated request_state
                request_states = scenario_state.get_request_states(train_trial_index, instance, None)
                if len(request_states) != 0:
                    instance_stats.extend(
                        self.evaluate_generation(
                            adapter_spec, singleton(request_states), metric_service, eval_cache_path
                        )
                    )

                # Evaluate the references
                request_states = []
                for reference_index in range(len(instance.references)):
                    request_states.extend(
                        scenario_state.get_request_states(train_trial_index, instance, reference_index)
                    )
                if len(request_states) != 0:
                    instance_stats.extend(
                        self.evaluate_references(adapter_spec, request_states, metric_service, eval_cache_path)
                    )

                # Add instance-related context (e.g., split, perturbation) to the metrics
                for i, stat in enumerate(instance_stats):
                    instance_stats[i] = add_context_from_instance(stat, instance)

                per_instance_stats[instance] = instance_stats

                # Merge these statistics back.
                for stat in instance_stats:
                    merge_stat(trial_stats, stat)

            # group stats according to the context (e.g., split, perturbation), i.e., non-name part of the MetricName,
            # and call derive_stats on each grouping
            grouped_trial_stats: Dict[MetricName, Dict[MetricName, Stat]] = defaultdict(dict)
            for metric_name, stat in trial_stats.items():
                grouped_trial_stats[replace(metric_name, name="none")][metric_name] = stat  # group by non-name fields
            for grouping_name, stats_dict in grouped_trial_stats.items():
                for stat in self.derive_stats(stats_dict):
                    # could skip this line if we want derive_stats to overwrite context, but this feels more robust
                    stat = Stat(replace(grouping_name, name=stat.name.name)).merge(stat)  # add correct context
                    merge_stat(trial_stats, stat)

            # same for per_instance_stats
            grouped_per_instance_stats: Dict[MetricName, Dict[Instance, List[Stat]]] = defaultdict(
                lambda: defaultdict(list)
            )
            for instance, stats in per_instance_stats.items():
                for stat in stats:
                    grouped_per_instance_stats[replace(stat.name, name="none")][instance].append(stat)
            for grouping_name, instance_dict in grouped_per_instance_stats.items():
                # Here, we assume that derive_per_instance_stats only computes trial_stats-level metrics
                # (instance-level metrics should be computed in the evaluate_{generation,references} anyway).
                for stat in self.derive_per_instance_stats(instance_dict):
                    stat = Stat(replace(grouping_name, name=stat.name.name)).merge(stat)  # add correct context
                    merge_stat(trial_stats, stat)

                # keep track of how many instances are in each subset
                merge_stat(trial_stats, Stat(replace(grouping_name, name="num_instances")).add(len(instance_dict)))

            # aggregate request states and call evaluate_instances in case the metric needs it
            grouped_request_states: Dict[MetricName, List[RequestState]] = defaultdict(list)
            for instance in scenario_state.instances:
                # TODO: do we need to support reference_index that is not None?
                grouping_name = add_context_from_instance(Stat(MetricName("none")), instance).name
                grouped_request_states[grouping_name].extend(
                    scenario_state.get_request_states(train_trial_index, instance, None)
                )

            for grouping_name, request_states in grouped_request_states.items():
                for stat in self.evaluate_instances(request_states):
                    stat = Stat(replace(grouping_name, name=stat.name.name)).merge(stat)  # make sure context is correct
                    merge_stat(trial_stats, stat)

            # This is here since we want these stats for all metrics and they aggregate across contexts (perturbations)
            worst_case_stats = self.compute_worst_case_metrics(per_instance_stats)
            for stat in worst_case_stats:
                merge_stat(trial_stats, stat)

            # We only take the mean value for each trial
            for stat in trial_stats.values():
                merge_stat(global_stats, stat.take_mean())

            for instance, instance_stats in per_instance_stats.items():
                all_per_instance_stats[PerInstanceStatsKey(instance, train_trial_index)] = instance_stats

        # Wrap aggregated and per-instance stats in a MetricResult.
        return MetricResult(list(global_stats.values()), all_per_instance_stats)

    def evaluate_generation(
        self,
        adapter_spec: AdapterSpec,
        request_state: RequestState,
        metric_service: MetricService,
        eval_cache_path: str,
    ) -> List[Stat]:
        """Evaluate free-form generation.  Override me!"""
        return []

    def evaluate_references(
        self,
        adapter_spec: AdapterSpec,
        reference_request_states: List[RequestState],
        metric_service: MetricService,
        eval_cache_path: str,
    ) -> List[Stat]:
        """Evaluate the references.  Override me!"""
        return []

    def evaluate_instances(self, request_states: List[RequestState]) -> List[Stat]:
        """Evaluate all request states directly. Use only if nothing else works.  Override me!"""
        return []

    def derive_stats(self, stats_dict: Dict[MetricName, Stat]) -> List[Stat]:
        """Derive stats based on existing stats, e.g., for perplexity. Override me!"""
        return []

    def derive_per_instance_stats(self, per_instance_stats: Dict[Instance, List[Stat]]) -> List[Stat]:
        """Derive stats based on existing per-instance stats, e.g., for calibration. Override me!"""
        return []

    def evaluate_language_modeling(
        self, scenario_state: ScenarioState, metric_service: MetricService, eval_cache_path: str
    ) -> MetricResult:
        global_stats: Dict[MetricName, Stat] = {}
        # The first and only trial
        trial_stats: Dict[MetricName, Stat] = {}
        # Per-instance stats
        all_per_instance_stats: Dict[PerInstanceStatsKey, List[Stat]] = {}
        instance_ids_per_group: Dict[MetricName, Set[str]] = defaultdict(set)

        for request_state in scenario_state.request_states:
            # Evaluate request_state
            request_stats = self.evaluate_generation(
                scenario_state.adapter_spec, request_state, metric_service, eval_cache_path
            )

            # Add instance-related context (e.g., split, perturbation) to the metrics
            for i, stat in enumerate(request_stats):
                stat = add_context_from_instance(stat, request_state.instance)
                request_stats[i] = stat
                assert request_state.instance.id is not None
                instance_ids_per_group[replace(stat.name, name="none")].add(request_state.instance.id)

            # Use trial index of 0 here since we run only one trial for LM
            all_per_instance_stats[PerInstanceStatsKey(request_state.instance, 0)] = request_stats

            for stat in request_stats:
                merge_stat(trial_stats, stat)

        # group stats according to the context (e.g., split, perturbation) and call derive_stats on each grouping
        grouped_trial_stats: Dict[MetricName, Dict[MetricName, Stat]] = defaultdict(dict)
        for metric_name, stat in trial_stats.items():
            grouping_name = replace(metric_name, name="none")  # only keep the non-name part of the metric_name
            grouped_trial_stats[grouping_name][metric_name] = stat

        for grouping_name, stats_dict in grouped_trial_stats.items():
            for stat in self.derive_stats(stats_dict):
                stat = Stat(replace(grouping_name, name=stat.name.name)).merge(stat)  # add correct context
                merge_stat(trial_stats, stat)
            # keep track of how many instances are in each subset
            instance_count: int = len(instance_ids_per_group[grouping_name])
            merge_stat(trial_stats, Stat(replace(grouping_name, name="num_instances")).add(instance_count))

        for stat in trial_stats.values():
            merge_stat(global_stats, stat.take_mean())
        return MetricResult(list(global_stats.values()), all_per_instance_stats)

    def compute_worst_case_metrics(self, per_instance_stats: Dict[Instance, List[Stat]]) -> List[Stat]:
        """
        For each instance, we compute the worst case perfomance between each perturbation and the non-perturbed input
        (identity perturbation). This allows us to reason about the invariances of a model as opposed to just looking
        at its performance on perturbed inputs. We also compute the worst case performance across all robustness-related
        and fairness-related perturbations (including identity in both).

        We returh the aggregate metrics across instances.
        """
        # Collect statistics per input-metric pair across perturbations
        per_instance_perturbation_stats: Dict[Tuple[MetricName, str], List[Stat]] = defaultdict(list)
        for instance, stats in per_instance_stats.items():
            for stat in stats:
                assert instance.id is not None
                # group all perturbations for a specific metric name together
                if stat.name.perturbation is not None:
                    per_instance_perturbation_stats[(replace(stat.name, perturbation=None), instance.id)].append(stat)

        # Compute worst perturbation stats
        derived_stats_dict: Dict[MetricName, Stat] = {}
        for (metric_name, instance_id), stats in per_instance_perturbation_stats.items():
            identity_stat: Optional[Stat] = None
            robustness_stat = Stat(
                replace(metric_name, perturbation=PerturbationDescription(name="robustness", robustness=True))
            )
            fairness_stat = Stat(
                replace(metric_name, perturbation=PerturbationDescription(name="fairness", fairness=True))
            )
            individual_perturbation_stats: Dict[PerturbationDescription, Stat] = {}

            for stat in stats:  # go through all the perturbations of the instance and merge relevant stats
                perturbation = stat.name.perturbation
                assert perturbation is not None
                if perturbation.name == "identity":
                    assert identity_stat is None  # we should only have one identity stat
                    identity_stat = stat
                else:
                    if perturbation.robustness:
                        robustness_stat.merge(stat)
                    if perturbation.fairness:
                        fairness_stat.merge(stat)
                    assert perturbation not in individual_perturbation_stats
                    individual_perturbation_stats[perturbation] = Stat(stat.name).merge(stat)  # copy

            for stat in [robustness_stat, fairness_stat, *individual_perturbation_stats.values()]:
                perturbation = stat.name.perturbation
                assert perturbation is not None

                if identity_stat is not None:
                    stat.merge(identity_stat)

                # keep the minimum performance for each input
                perturbation = replace(perturbation, name=f"worst_{perturbation.name}")
                if stat.count > 0:
                    merge_stat(derived_stats_dict, Stat(replace(stat.name, perturbation=perturbation)).add(stat.min))
        return list(derived_stats_dict.values())


class MetricSpec(ObjectSpec):
    """Specifies how to create a `Metric`."""

    pass


def create_metric(metric_spec: MetricSpec) -> Metric:
    return create_object(metric_spec)


def get_all_stats_by_name(stats: Union[List[Stat], Dict[Any, Stat]], name: str) -> List[Stat]:
    """Returns a list of all stats with the specified name."""
    stats_list: List[Stat] = []
    if isinstance(stats, list):
        stats_list = stats
    else:
        stats_list = list(stats.values())
    return [stat for stat in stats_list if stat.name.name == name]


def get_unique_stat_by_name(stats: Union[List[Stat], Dict[Any, Stat]], name: str) -> Optional[Stat]:
    """Returns the unique stat with the specified name or None if it's not there."""
    matching_stats: List[Stat] = get_all_stats_by_name(stats, name)
    if len(matching_stats) == 0:
        return None
    return singleton(matching_stats)


def add_context_from_instance(stat: Stat, instance: Instance) -> Stat:
    """Populate the fields of the Stat with the context info (e.g., split, perturbation) from the instance."""
    return Stat(
        replace(stat.name, split=instance.split, sub_split=instance.sub_split, perturbation=instance.perturbation,)
    ).merge(stat)
