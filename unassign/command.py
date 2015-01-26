import logging
import optparse
import sys


from unassign.search_blast import (
    blast_to, top_hits, hit_identity, group_by_query,
    )
from unassign.parse import parse_fasta, write_fasta
from unassign.util import uniq


class Unassigner(object):
    def __init__(self, species_fp, refseqs_fp, aligner):
        self.species_fp = species_fp
        self.refseqs_fp = refseqs_fp
        self.aligner = aligner

    def unassign(self, query_fp):
        """Execute unassignment algorithm on a filepath of query seqs.
        """
        logging.info("Aligning query seqs to type strain seqs")
        species_hits = self.aligner.align_query_to_typestrains(
                query_fp, self.species_fp)

        logging.info("Aligning type strain seqs to reference seqs")
        species_hit_seqs = self._get_species_seqs(species_hits)
        refseq_hits = self.aligner.align_typestrain_to_refseqs(
                species_hit_seqs, self.refseqs_fp)

        logging.info("Evaluating aignment results")
        grouped_refseq_hits = group_by_query(refseq_hits)
        r_hits_species_id, r_hits = next(grouped_refseq_hits)
        for s_hit in species_hits:
            query_id = s_hit['qseqid']
            species_id = s_hit['sseqid']
            if species_id != r_hits_species_id:
                r_hits_species_id, r_hits = next(grouped_refseq_hits)
            if species_id != r_hits_species_id:
                raise RuntimeError(
                    "Missing reference hits for %s" % species_id)
            self._evaluate_confidence(s_hit, r_hits)

    def _get_species_seqs(self, species_hits):
        """Fetch seqs for each species in the list of hits.

        Each unique species in the list is returned only once in the
        list of results, but the order of hits is preserved to
        facilitate caching.
        """
        species_hit_ids = uniq(x['sseqid'] for x in species_hits)
        strain_seqs = self._load_type_strain_seqs()
        return [(x, strain_seqs[x]) for x in species_hit_ids]
            
    def _evaluate_confidence(self, species_hit, refseq_hits):
        query_id = species_hit['qseqid']
        species_id = species_hit['sseqid']
        start = species_hit['sstart']
        end = species_hit['send']
        for r_hit in refseq_hits:
            refseq_id = r_hit['sseqid']
            a, b = hit_identity(r_hit, start, end)
            c, d = hit_identity(r_hit)
            print query_id, species_id, a, b, refseq_id, c, d

    def _load_type_strain_seqs(self):
        with open(self.species_fp) as f:
            recs = parse_fasta(f)
            return dict((desc.split()[0], seq) for desc, seq in recs)


def main(argv=None):
    p = optparse.OptionParser()
    p.add_option("--query_fp", help=(
        "Query sequences filepath (FASTA format) [REQUIRED]"))
    p.add_option("--type_strain_fp", default="species.fasta", help=(
        "Type strain sequences filepath (FASTA format + BLAST database) "
        "[default: %default]"))
    p.add_option("--reference_fp", default="refseqs.fasta", help=(
        "Reference sequence filepath (FASTA format + BLAST database) "
        "[default: %default]"))
    p.add_option("--num_cpus", type="int", default=1, help=(
        "Number of CPUs to use in seqrch and alignment steps "
        "[default: %default]"))
    p.add_option("--verbose", action="store_true", help=(
        "Activate verbose mode."))
    opts, args = p.parse_args(argv)

    if opts.verbose is True:
        logging.basicConfig(level=logging.INFO)

    a = BlastAligner(opts.num_cpus)
    u = Unassigner(opts.type_strain_fp, opts.reference_fp, a)
    u.unassign(opts.query_fp)