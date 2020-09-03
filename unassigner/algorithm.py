import collections
import itertools
import math
import operator

import scipy
import scipy.special

from unassigner.alignment import AlignedRegion
from unassigner.align import VsearchAligner, HitExtender
from unassigner.parse import parse_fasta

def beta_binomial_pdf(k, n, alpha, beta):
    binom_coeff = scipy.special.comb(n, k)
    if binom_coeff == 0:
        return 0
    t1 = math.log(binom_coeff)
    t2 = scipy.special.betaln(k + alpha, n - k + beta)
    t3 = scipy.special.betaln(alpha, beta)
    logf = t1 + t2 - t3
    return math.exp(logf)


def beta_binomial_cdf(k_max, n, alpha, beta):
    k = 0
    val = 0
    while k <= k_max:
        val += beta_binomial_pdf(k, n, alpha, beta)
        k += 1
    return val


class UnassignAligner(object):
    def __init__(self, species_fp):
        self.species_fp = species_fp
        self.species_input_fp = None
        self.species_output_fp = None
        self.num_cpus = None

    def search_species(self, query_seqs):
        b = VsearchAligner(self.species_fp)
        vsearch_args = {
            "min_id": 0.9,
            "maxaccepts": 5,
        }
        if self.num_cpus:
            vsearch_args["threads"] = self.num_cpus
        hits = b.search(
            query_seqs,
            self.species_input_fp, self.species_output_fp, **vsearch_args)

        with open(self.species_fp) as f:
            ref_seqs = list(parse_fasta(f, trim_desc=True))
        xt = HitExtender(query_seqs, ref_seqs)
        for hit in hits:
            yield xt.extend_hit(hit)


class FileAligner:
    def __init__(self, species_fp, output_fp):
        self.species_fp = species_fp
        self.output_fp = output_fp

    def search_species(self, seqs):
        with open(self.species_fp) as f:
            ref_seqs = list(parse_fasta(f, trim_desc=True))
        xt = HitExtender(seqs, ref_seqs)
        with open(self.output_fp) as of:
            hits = VsearchAligner._parse(of)
            for hit in hits:
                yield xt.extend_hit(hit)


class UnassignerAlgorithm:
    def __init__(self, aligner):
        self.aligner = aligner
        self.alignment_min_percent_id = 0.975

    def unassign(self, query_seqs):
        query_seqs = list(query_seqs)
        query_ids = [seq_id for seq_id, seq in query_seqs]

        # Steps in algorithm:
        # 1. Align query sequences to type strain sequences
        alignments = self._align_query_to_type_strain(query_seqs)

        # 3. For each query-type strain alignment,
        #    estimate distribution of mismatches outside fragment
        mm_distributions = self._estimate_mismatch_distributions(alignments)

        # 4. For each query-type strain alignment,
        #    estimate unassignment probability
        unassignments = self._estimate_unassignment_probabilities(mm_distributions)

        # 5. Group by query and yield results to caller
        alignments_by_query = collections.defaultdict(list)
        for a in alignments:
            alignments_by_query[a.query_id].append(a)
        for query_id in query_ids:
            query_alignments = alignments_by_query[query_id]
            results = [self._get_indiv_probability(a) for a in query_alignments]
            if not results:
                results = [self.null_result]
            yield query_id, results

    def _align_query_to_type_strain(self, query_seqs):
        # We expect query_seqs to be a list
        query_ids = [seq_id for seq_id, seq in query_seqs]
        unsorted_alignments = self.aligner.search_species(query_seqs)

        alignments_by_query = collections.defaultdict(list)
        for a in unsorted_alignments:
            alignments_by_query[a.query_id].append(a)

        for query_id in query_ids:
            query_alignments = alignments_by_query[query_id]
            query_alignments = self._filter_alignments(query_alignments)
            for a in query_alignments:
                yield a

    def _filter_alignments(self, query_alignments):
        sorted_alignments = list(sorted(
            query_alignments, key=operator.attrgetter('percent_id'),
            reverse=True))
        filtered_alignments = [
            a for a in sorted_alignments
            if a.percent_id > self.alignment_min_percent_id]
        # Return one low-identity result if we have nothing better
        if sorted_alignments and not filtered_alignments:
            return sorted_alignments[:1]
        return filtered_alignments

    def _estimate_mismatch_distributions(self, alignments):
        # constant vs. variable mismatch rate
        pass

    def _estimate_unassignment_probabilities(self, mm_distributions):
        # hard vs. soft unassignment threshold
        pass


class ThresholdAlgorithm(UnassignerAlgorithm):
    """Threshold algorithm for species unassignment

    In this algorithm, we set a threshold value for sequence
    similarity to the type strain sequence.  For a query sequence, we
    calculate the probability of falling below this similarity
    threshold over the full length of the 16S gene.  This value is the
    unassignment probability.
    """
    result_keys = [
        "typestrain_id", "probability_incompatible", "region_mismatches",
        "region_positions", "region_matches", "nonregion_positions_in_subject",
        "max_nonregion_mismatches",
    ]
    null_result = dict((key, "NA") for key in result_keys)

    def __init__(self, aligner):
        super().__init__(aligner)
        self.prior_alpha = 0.5
        self.prior_beta = 0.5
        self.species_threshold = 0.975

    def _get_indiv_probability(self, alignment):
        region = AlignedRegion.without_endgaps(alignment).trim_ends()
        region_positions = region.alignment_len
        region_matches = region.count_matches()
        region_mismatches = region_positions - region_matches

        alpha = region_mismatches + self.prior_alpha
        beta = region_matches + self.prior_beta

        nonregion_subject_positions = (
            alignment.subject_len - region.subject_len)
        total_positions = (
            region_positions + nonregion_subject_positions)

        species_mismatch_threshold = 1 - self.species_threshold
        max_total_mismatches = int(math.floor(
            species_mismatch_threshold * total_positions))
        max_nonregion_mismatches = max_total_mismatches - region_mismatches

        prob_compatible = beta_binomial_cdf(
            max_nonregion_mismatches, nonregion_subject_positions, alpha, beta)
        prob_incompatible = 1 - prob_compatible

        return {
            "typestrain_id": alignment.subject_id,
            "probability_incompatible": prob_incompatible,
            "region_mismatches": region_mismatches,
            "region_positions": region_positions,
            "region_matches": region_matches,
            "nonregion_positions_in_subject": nonregion_subject_positions,
            "max_nonregion_mismatches": max_nonregion_mismatches,
        }
