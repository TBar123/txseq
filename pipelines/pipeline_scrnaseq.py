##############################################################################
#
#   Kennedy Institute of Rheumatology
#
#   $Id$
#
#   Copyright (C) 2015 Stephen Sansom
#
#   This program is free software; you can redistribute it and/or
#   modify it under the terms of the GNU General Public License
#   as published by the Free Software Foundation; either version 2
#   of the License, or (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, write to the Free Software
#   Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
###############################################################################

"""===========================
Pipeline template
===========================

:Author: Stephen Sansom
:Release: $Id$
:Date: |today|
:Tags: Python


Overview
========

This pipeline performs the follow tasks:

(1) [optional] Mapping of reads using hisat
      - paired (default) or single end fastq files are expected as the input
      - unstranded (default) or stranded fastq files are expected as the input
      - If data is already mapped position sorted, indexed BAM files can be
        provided instead.
      - See pipeline.ini and below for filename syntax guidance.

(2) Quantitation of gene expression
      - Ensembl protein coding + ERCC spikes
      - Cufflinks (cuffquant + cuffnorm) is run for copy number estimation
      - HTseq is run for counts

(3) Calculation of post-mapping QC statistics


Usage
=====

See :ref:`PipelineSettingUp` and :ref:`PipelineRunning` on general
information how to use CGAT pipelines.

Configuration
-------------

The pipeline requires a configured :file:`pipeline.ini` file.
CGATReport report requires a :file:`conf.py` and optionally a
file:`cgatreport.ini` file (see :ref:`PipelineReporting`).

Default configuration files can be generated by executing:

   python <srcdir>/pipeline_singlecell.py config


Input files
-----------

* Fastqs

- The pipeline expects sequence data from each cell in the form
  of single or paired-end fastq files to be present in the "fastq_dir"
  specificed in the pipeline.ini file (default = "data.dir")

* BAMs

- location of directory containing the BAM files is specified in the
  "bam_dir" variable in pipeline.ini (default = "data.dir")

* Naming

It is recommended that files are named according
to the following convention (for plate-based single-cell data):

source-condition-replicate-plate-row-column

An arbitrary number of fields can be specified, e.g. see the
Pipeline.ini default:

name_field_separator=-
name_field_titles=source,condition,plate,row,column

example fastq file name:

mTEChi-wildtype-plate1-A-1.fastq.1.gz

Where BAM files are provided, the mapper is expected as a suffix, i.e.

mTEChi-wildtype-plate1-A-1.gsnap.bam
mTEChi-wildtype-plate1-A-1.gsnap.bam.bai

* ERCC information

The location of a table of ERCC spike in copy numbers should be
provided in the pipeline.ini file.

The expected structure (tab-delimited) is:

gene_id|genebank_id|5prime_assay |3prime_assay |sequence|length|copies_per_cell
ERCC-..|EF011072   |Ac03459943_a1|Ac03460039_a1|CGAT... |1059  |20324.7225


Requirements
------------

The pipeline requires the results from
:doc:`pipeline_annotations`. Set the configuration variable
:py:data:`annotations_database` and :py:data:`annotations_dir`.

On top of the default CGAT setup, the pipeline requires the following
software to be in the path:

.. Add any additional external requirements such as 3rd party software
   or R modules below:

Requirements (TBC):

* cufflinks
* picard
* hisat
* htseq
* R
* etc!

Pipeline output
===============

.. TBC

Glossary
========

.. glossary::


Code
====

"""
from ruffus import *

import sys
import shutil
import os
import glob
import sqlite3

import pandas as pd
import numpy as np

import CGAT.Experiment as E
import CGATPipelines.Pipeline as P
import CGAT.Database as DB

import PipelineScRnaseq as PipelineScRnaseq

# -------------------------- < parse parameters > --------------------------- #

# load options from the config file
PARAMS = P.getParameters(
    ["%s/pipeline.ini" % os.path.splitext(__file__)[0],
     "../pipeline.ini",
     "pipeline.ini"])

# add configuration values from associated pipelines
#
# 1. pipeline_annotations: any parameters will be added with the
#    prefix "annotations_". The interface will be updated with
#    "annotations_dir" to point to the absolute path names.
PARAMS.update(P.peekParameters(
    PARAMS["annotations_dir"],
    "pipeline_annotations.py",
    on_error_raise=__name__ == "__main__",
    prefix="annotations_",
    update_interface=True))


# if necessary, update the PARAMS dictionary in any modules file.
# e.g.:
#
# import CGATPipelines.PipelineGeneset as PipelineGeneset
# PipelineGeneset.PARAMS = PARAMS
#
# Note that this is a hack and deprecated, better pass all
# parameters that are needed by a function explicitely.

# Establish the location of module scripts for P.submit() functions
if PARAMS["code_dir"] == "":
    code_dir = os.path.dirname(os.path.realpath(__file__))
else:
    code_dir = PARAMS["code_dir"]


# ------------------------- < utility functions > --------------------------- #

def connect():
    '''utility function to connect to database.

    Use this method to connect to the pipeline database.
    Additional databases can be attached here as well.

    Returns an sqlite3 database handle.
    '''

    dbh = sqlite3.connect(PARAMS["database_name"])
    statement = '''ATTACH DATABASE '%s' as annotations''' % (
        PARAMS["annotations_database"])
    cc = dbh.cursor()
    cc.execute(statement)
    cc.close()

    return dbh


# ########################################################################### #
# ########### Define endedness and strandedness parameters ################## #
# ########################################################################### #

# determine endedness
if str(PARAMS["paired"]).lower() in ("1", "true", "yes"):
    PAIRED = True
elif str(PARAMS["paired"]).lower() in ("0", "false", "no"):
    PAIRED = False
else:
    raise ValueError("Endedness not recognised")

# set options based on strandedness
STRAND = str(PARAMS["strandedness"]).lower()
if STRAND not in ("none", "forward", "reverse"):
    raise ValueError("Strand not recognised")

if STRAND == "none":
    CUFFLINKS_STRAND = "fr-unstranded"
    HTSEQ_STRAND = "no"
    PICARD_STRAND = "NONE"

elif STRAND == "forward":
    if PAIRED:
        HISAT_STRAND = "FR"
    else:
        HISAT_STRAND = "F"
    CUFFLINKS_STRAND = "fr-secondstrand"
    HTSEQ_STRAND = "yes"
    PICARD_STRAND = "FIRST_READ_TRANSCRIPTION_STRAND"

elif STRAND == "reverse":
    if PAIRED:
        HISAT_STRAND = "RF"
    else:
        HISAT_STRAND = "R"
    CUFFLINKS_STRAND = "fr-firststrand"
    HTSEQ_STRAND = "reverse"
    PICARD_STRAND = "SECOND_READ_TRANSCRIPTION_STRAND"


# ---------------------- < specific pipeline tasks > ------------------------ #

# ########################################################################### #
# #################### (1) Read Mapping (optional) ########################## #
# ########################################################################### #


if PAIRED:
        fastq_pattern = "*.fastq.1.gz"
else:
        fastq_pattern = "*.fastq.gz"

if STRAND != "none":
    HISAT_STRAND_PARAM = "--rna-strandness %s" % HISAT_STRAND
else:
    HISAT_STRAND_PARAM = ""


@follows(mkdir("hisat.dir/first.pass.dir"))
@transform(glob.glob(os.path.join(PARAMS["fastq_dir"], fastq_pattern)),
           regex(r".*/(.*).fastq.*.gz"),
           r"hisat.dir/first.pass.dir/\1.novel.splice.sites.txt.gz")
def hisatFirstPass(infile, outfile):
    '''Run a first hisat pass to identify novel splice sites'''

    reads_one = infile

    index = PARAMS["hisat_index"]
    threads = PARAMS["hisat_threads"]
    log = outfile + ".log"
    out_name = outfile[:-len(".gz")]

    # queue options
    to_cluster = True  # this is the default
    job_threads = threads
    job_options = "-l mem_free=4G"

    if PAIRED:
        reads_two = reads_one.replace(".1.", ".2.")
        fastq_input = "-1 " + reads_one + " -2 " + reads_two
    else:
        fastq_input = "-U " + reads_one

    hisat_strand_param = HISAT_STRAND_PARAM

    statement = '''%(hisat_executable)s
                      -x %(index)s
                      %(fastq_input)s
                      --threads %(threads)s
                      --novel-splicesite-outfile %(out_name)s
                      %(hisat_strand_param)s
                      %(hisat_options)s
                      -S /dev/null
                   &> %(log)s;
                   checkpoint;
                   gzip %(out_name)s;
                 '''

    P.run()


@follows(mkdir("annotations.dir"))
@merge(hisatFirstPass, "annotations.dir/novel.splice.sites.hisat.txt")
def novelHisatSpliceSites(infiles, outfile):
    '''Collect the novel splice sites into a single file'''

    junction_files = " ".join(infiles)

    statement = '''zcat %(junction_files)s
                   | sort -k1,1 | uniq
                   > %(outfile)s
                '''

    P.run()


@transform(glob.glob("data.dir/" + fastq_pattern),
           regex(r".*/(.*).fastq.*.gz"),
           add_inputs(novelHisatSpliceSites),
           r"hisat.dir/\1.hisat.bam")
def hisatAlignments(infiles, outfile):
    '''Align reads using hisat with known and novel junctions'''

    reads_one, novel_splice_sites = infiles

    out_sam = P.getTempFilename()
    index = PARAMS["hisat_index"]
    threads = PARAMS["hisat_threads"]
    log = outfile + ".log"
    outname = outfile[:-len(".bam")]

    to_cluster = True
    job_threads = threads
    job_options = "-l mem_free=4G"

    if PAIRED:
        reads_two = reads_one.replace(".1.", ".2.")
        fastq_input = "-1 " + reads_one + " -2 " + reads_two
    else:
        fastq_input = "-U " + reads_one

    hisat_strand_param = HISAT_STRAND_PARAM

    statement = '''%(hisat_executable)s
                      -x %(index)s
                      %(fastq_input)s
                      --threads %(threads)s
                      --novel-splicesite-infile %(novel_splice_sites)s
                      %(hisat_strand_param)s
                      %(hisat_options)s
                      -S %(out_sam)s
                   &> %(log)s;
                   checkpoint;
                   samtools view -bS %(out_sam)s
                   | samtools sort - %(outname)s >>%(log)s;
                   checkpoint;
                   samtools index %(outfile)s;
                   checkpoint;
                   rm %(out_sam)s;
                 '''

    P.run()


@follows(hisatAlignments)
def mapping():
    '''mapping target'''
    pass


# ########################################################################### #
# ########### Collect BAMs from mapping functions or inputs  ################ #
# ########################################################################### #

if PARAMS["input"] == "fastq":
    collectBAMs = hisatAlignments

elif PARAMS["input"] == "bam":
    collectBAMs = glob.glob(os.path.join(PARAMS["bam_dir"], "*.bam"))

else:
    raise ValueError('Input type must be either "fastq" or "bam"')


# ########################################################################### #
# ################ (2) Quantification of gene expression #################### #
# ########################################################################### #

# ------------------------- Geneset Definition ------------------------------ #

@follows(mkdir("annotations.dir"))
@files((os.path.join(PARAMS["annotations_dir"],
                     PARAMS["annotations_ensembl_geneset"]),
        PARAMS["annotations_ercc92_geneset"]),
       "annotations.dir/ens_ercc_geneset.gtf.gz")
def prepareEnsemblERCC92GTF(infiles, outfile):
        '''Preparation of geneset for quantitation.
           ERCC92 GTF entries are appended to
           the protein coding entries from Ensembl geneset_all'''

        ensembl, ercc = infiles

        outname = outfile[:-len(".gz")]

        statement = ''' zgrep 'gene_biotype "protein_coding"' %(ensembl)s
                        > %(outname)s;
                        checkpoint;
                        zcat %(ercc)s >> %(outname)s;
                        checkpoint;
                        gzip %(outname)s;
                    '''
        P.run()


# ----------------------------- Read Counting ------------------------------- #

@follows(mkdir("htseq.dir"))
@transform(collectBAMs,
           regex(r".*/(.*).bam"),
           add_inputs(prepareEnsemblERCC92GTF),
           r"htseq.dir/\1.counts")
def runHTSeq(infiles, outfile):
    '''Run htseq-count'''

    bamfile, gtf = infiles
    htseq_strand = HTSEQ_STRAND

    statement = ''' htseq-count
                        -f bam
                        -r pos
                        -s %(htseq_strand)s
                        -t exon
                        --quiet
                        %(bamfile)s %(gtf)s >
                        %(outfile)s; '''
    P.run()


@merge(runHTSeq,
       "htseq.dir/htseq_counts.load")
def loadHTSeqCounts(infiles, outfile):

        P.concatenateAndLoad(infiles, outfile,
                             regex_filename=".*/(.*).counts",
                             has_titles=False,
                             cat="track",
                             header="track,gene_id,counts",
                             options='-i "gene_id"')


# -------------------- FPKM (Cufflinks) quantitation------------------------- #

@follows(mkdir("cuffquant.dir"))
@transform(collectBAMs,
           regex(r".*/(.*).bam"),
           add_inputs(prepareEnsemblERCC92GTF),
           r"cuffquant.dir/\1.log")
def cuffQuant(infiles, outfile):
    '''Per sample quantification using cuffquant'''

    bam_file, geneset = infiles

    # because the output of cuffquant is always "abundances.cxb"
    # a unique output directory for each sample is required
    output_dir = outfile[:-len(".log")]

    job_threads = PARAMS["cufflinks_cuffquant_threads"]  # 4

    to_cluster = True

    genome_multifasta = os.path.join(PARAMS["annotations_genome_dir"],
                                     PARAMS["genome"]+".fasta")

    gtf = P.getTempFilename()
    cufflinks_strand = CUFFLINKS_STRAND

    statement = '''zcat %(geneset)s > %(gtf)s;
                   checkpoint;
                   cuffquant
                           --output-dir %(output_dir)s
                           --num-threads %(job_threads)s
                           --multi-read-correct
                           --library-type %(cufflinks_strand)s
                           --no-effective-length-correction
                           --max-bundle-frags 2000000
                           --max-mle-iterations 10000
                           --verbose
                           --frag-bias-correct %(genome_multifasta)s
                            %(gtf)s %(bam_file)s >& %(outfile)s;
                    checkpoint;
                    rm %(gtf)s;
                '''

    P.run()


@follows(mkdir("cuffnorm.dir"), cuffQuant)
@merge([prepareEnsemblERCC92GTF, cuffQuant],
       "cuffnorm.dir/cuffnorm.log")
def cuffNorm(infiles, outfile):
    '''Calculate FPKMs using cuffNorm'''

    # parse the infiles
    geneset = infiles[0]

    cxb_files = " ".join([f[:-len(".log")] + "/abundances.cxb"
                          for f in infiles[1:]])

    # get the output directory and cell labels
    output_dir = os.path.dirname(outfile)

    labels = ",".join([f.split("/")[1]
                       for f in cxb_files.split(" ")])

    job_options = "-l mem_free=8G"
    job_threads = PARAMS["cufflinks_cuffnorm_threads"]

    gtf = P.getTempFilename()
    cufflinks_strand = CUFFLINKS_STRAND

    statement = ''' zcat %(geneset)s > %(gtf)s;
                    checkpoint;
                    cuffnorm
                        --output-dir %(output_dir)s
                        --num-threads=%(job_threads)s
                        --library-type %(cufflinks_strand)s
                        --total-hits-norm
                        --library-norm-method classic-fpkm
                        --labels %(labels)s
                        %(gtf)s %(cxb_files)s > %(outfile)s;
                     checkpoint;
                     rm %(gtf)s;
                '''

    P.run()


@transform(cuffNorm,
           suffix(".log"),
           ".load")
def loadCuffNorm(infile, outfile):
    '''load the fpkm table from cuffnorm into the database'''

    fpkm_table = os.path.dirname(infile) + "/genes.fpkm_table"

    P.load(fpkm_table, outfile,
           options='-i "gene_id"')


# ---------------------- Copynumber estimation ------------------------------ #

@follows(mkdir("annotations.dir"))
@files(PARAMS["annotations_ercc92_info"],
       "annotations.dir/ercc.load")
def loadERCC92Info(infile, outfile):
    '''load the spike-in info including copy number'''

    P.load(infile, outfile, options='-i "gene_id"')


@follows(mkdir("copy.number.dir"))
@transform(cuffQuant,
           regex(r".*/(.*).log"),
           add_inputs(loadCuffNorm,
                      loadERCC92Info),
           r"copy.number.dir/\1.copynumber")
def estimateCopyNumber(infiles, outfile):
    '''Estimate copy numbers based on standard
       curves constructed from the spike-ins'''

    P.submit(os.path.join(code_dir, "PipelineScRnaseq.py"),
             "estimateCopyNumber",
             infiles=infiles,
             outfiles=outfile,
             params=[code_dir])


@merge(estimateCopyNumber, "copy.number.dir/copynumber.load")
def loadCopyNumber(infiles, outfile):
    '''load the copy number estimations to the database'''

    P.concatenateAndLoad(infiles, outfile,
                         regex_filename=".*/(.*).copynumber",
                         options='-i "gene_id"')


@follows(loadCopyNumber, loadHTSeqCounts)
def quantitation():
    '''quantitation target'''
    pass


# ########################################################################### #
# ################ (3) Post-mapping Quality Control ######################### #
# ########################################################################### #


# ------------------- Picard: CollectRnaSeqMetrics -------------------------- #

@follows(mkdir("qc.dir/rnaseq.metrics.dir/"))
@transform(collectBAMs,
           regex(r".*/(.*).bam"),
           r"qc.dir/rnaseq.metrics.dir/\1.rnaseq.metrics")
def collectRnaSeqMetrics(infile, outfile):
    '''Run Picard CollectRnaSeqMetrics on the bam files'''

    picard_out = P.getTempFilename()
    picard_options = PARAMS["picard_collectrnaseqmetrics_options"]

    geneset_flat = PARAMS["picard_geneset_flat"]
    validation_stringency = PARAMS["picard_validation_stringency"]

    job_threads = PARAMS["picard_threads"]
    job_options = "-l mem_free=" + PARAMS["picard_memory"]

    coverage_out = outfile[:-len(".metrics")] + ".cov.hist"
    chart_out = outfile[:-len(".metrics")] + ".cov.pdf"

    picard_strand = PICARD_STRAND

    statement = '''CollectRnaSeqMetrics
                   I=%(infile)s
                   REF_FLAT=%(geneset_flat)s
                   O=%(picard_out)s
                   CHART=%(chart_out)s
                   STRAND_SPECIFICITY=%(picard_strand)s
                   VALIDATION_STRINGENCY=%(validation_stringency)s
                   %(picard_options)s;
                   checkpoint;
                   grep . %(picard_out)s | grep -v "#" | head -n2
                   > %(outfile)s;
                   checkpoint;
                   grep . %(picard_out)s
                   | grep -A 102 "## HISTOGRAM"
                   | grep -v "##"
                   > %(coverage_out)s;
                   checkpoint;
                   rm %(picard_out)s;
                ''' % locals()

    P.run()


@merge(collectRnaSeqMetrics,
       "qc.dir/qc_rnaseq_metrics.load")
def loadCollectRnaSeqMetrics(infiles, outfile):
    '''load the metrics to the db'''

    P.concatenateAndLoad(infiles, outfile,
                         regex_filename=".*/.*/(.*).rnaseq.metrics",
                         cat="cell",
                         options='-i "cell"')


# --------------------- Three prime bias analysis --------------------------- #

@transform(collectRnaSeqMetrics,
           suffix(".rnaseq.metrics"),
           ".three.prime.bias")
def threePrimeBias(infile, outfile):
    '''compute a sensible three prime bias metric
       from the picard coverage histogram'''

    coverage_histogram = infile[:-len(".metrics")] + ".cov.hist"

    df = pd.read_csv(coverage_histogram, sep="\t")

    x = "normalized_position"
    cov = "All_Reads.normalized_coverage"

    three_prime_coverage = np.mean(df[cov][(df[x] > 70) & (df[x] < 90)])
    transcript_body_coverage = np.mean(df[cov][(df[x] > 20) & (df[x] < 90)])
    bias = three_prime_coverage / transcript_body_coverage

    with open(outfile, "w") as out_file:
        out_file.write("three_prime_bias\n")
        out_file.write("%.2f\n" % bias)


@merge(threePrimeBias,
       "qc.dir/qc_three_prime_bias.load")
def loadThreePrimeBias(infiles, outfile):
    '''load the metrics to the db'''

    P.concatenateAndLoad(infiles, outfile,
                         regex_filename=".*/.*/(.*).three.prime.bias",
                         cat="cell",
                         options='-i "cell"')


# ----------------- Picard: EstimateLibraryComplexity ----------------------- #

@follows(mkdir("qc.dir/library.complexity.dir/"))
@transform(collectBAMs,
           regex(r".*/(.*).bam"),
           r"qc.dir/library.complexity.dir/\1.library.complexity")
def estimateLibraryComplexity(infile, outfile):
    '''Run Picard EstimateLibraryComplexity on the bam files'''

    if PAIRED:
        picard_out = P.getTempFilename()
        picard_options = PARAMS["picard_estimatelibrarycomplexity_options"]

        validation_stringency = PARAMS["picard_validation_stringency"]

        job_threads = PARAMS["picard_threads"]
        job_options = "-l mem_free=" + PARAMS["picard_memory"]

        statement = '''EstimateLibraryComplexity
                       I=%(infile)s
                       O=%(picard_out)s
                       VALIDATION_STRINGENCY=%(validation_stringency)s
                       %(picard_options)s;
                       checkpoint;
                       grep . %(picard_out)s | grep -v "#" | head -n2
                       > %(outfile)s;
                       checkpoint;
                       rm %(picard_out)s;
                    ''' % locals()

    else:
        statement = '''echo "Not compatible with SE data"
                       > %(outfile)s'''

    P.run()


@merge(estimateLibraryComplexity,
       "qc.dir/qc_library_complexity.load")
def loadEstimateLibraryComplexity(infiles, outfile):
    '''load the complexity metrics to a single table in the db'''

    if PAIRED:
        P.concatenateAndLoad(infiles, outfile,
                             regex_filename=".*/.*/(.*).library.complexity",
                             cat="cell",
                             options='-i "cell"')
    else:
        statement = '''echo "Not compatible with SE data"
                       > %(outfile)s'''
        P.run()


# ------------------- Picard: AlignmentSummaryMetrics ----------------------- #

@follows(mkdir("qc.dir/alignment.summary.metrics.dir/"))
@transform(collectBAMs,
           regex(r".*/(.*).bam"),
           (r"qc.dir/alignment.summary.metrics.dir"
            r"/\1.alignment.summary.metrics"))
def alignmentSummaryMetrics(infile, outfile):
    '''Run Picard AlignmentSummaryMetrics on the bam files'''

    picard_out = P.getTempFilename()
    picard_options = PARAMS["picard_alignmentsummarymetric_options"]
    validation_stringency = PARAMS["picard_validation_stringency"]

    job_threads = PARAMS["picard_threads"]
    job_options = "-l mem_free=" + PARAMS["picard_memory"]

    reference_sequence = os.path.join(PARAMS["annotations_genome_dir"],
                                      PARAMS["genome"] + ".fasta")

    statement = '''CollectAlignmentSummaryMetrics
                   I=%(infile)s
                   O=%(picard_out)s
                   REFERENCE_SEQUENCE=%(reference_sequence)s
                   VALIDATION_STRINGENCY=%(validation_stringency)s
                   %(picard_options)s;
                   checkpoint;
                   grep . %(picard_out)s | grep -v "#"
                   > %(outfile)s;
                   checkpoint;
                   rm %(picard_out)s;
                ''' % locals()

    P.run()


@merge(alignmentSummaryMetrics,
       "qc.dir/qc_alignment_summary_metrics.load")
def loadAlignmentSummaryMetrics(infiles, outfile):
    '''load the complexity metrics to a single table in the db'''

    P.concatenateAndLoad(infiles, outfile,
                         regex_filename=".*/.*/(.*).alignment.summary.metrics",
                         cat="cell",
                         options='-i "cell"')


# ------------------- Picard: InsertSizeMetrics ----------------------- #

@follows(mkdir("qc.dir/insert.size.metrics.dir/"))
@transform(collectBAMs,
           regex(r".*/(.*).bam"),
           [(r"qc.dir/insert.size.metrics.dir"
             r"/\1.insert.size.metrics.summary"),
            (r"qc.dir/insert.size.metrics.dir"
             r"/\1.insert.size.metrics.histogram")])
def insertSizeMetricsAndHistograms(infile, outfiles):
    '''Run Picard InsertSizeMetrics on the BAM files to
       collect summary metrics and histograms'''

    if PAIRED:
        picard_summary, picard_histogram = outfiles
        picard_out = P.getTempFilename()
        picard_histogram_pdf = picard_histogram + ".pdf"
        picard_options = PARAMS["picard_insertsizemetric_options"]

        job_threads = PARAMS["picard_threads"]
        job_options = "-l mem_free=" + PARAMS["picard_memory"]

        validation_stringency = PARAMS["picard_validation_stringency"]
        reference_sequence = os.path.join(PARAMS["annotations_genome_dir"],
                                          PARAMS["genome"] + ".fasta")

        statement = '''CollectInsertSizeMetrics
                       I=%(infile)s
                       O=%(picard_out)s
                       HISTOGRAM_FILE=%(picard_histogram_pdf)s
                       VALIDATION_STRINGENCY=%(validation_stringency)s
                       REFERENCE_SEQUENCE=%(reference_sequence)s
                       %(picard_options)s;
                       checkpoint;
                       grep "MEDIAN_INSERT_SIZE" -A 1 %(picard_out)s
                       > %(picard_summary)s;
                       checkpoint;
                       sed -e '1,/## HISTOGRAM/d' %(picard_out)s
                       > %(picard_histogram)s;
                       checkpoint;
                       rm %(picard_out)s;
                    ''' % locals()

    else:
        picard_summary, picard_histogram = outfiles

        statement = '''echo "Not compatible with SE data"
                       > %(picard_summary)s;
                       checkpoint;
                       echo "Not compatible with SE data"
                       > %(picard_histogram)s
                    ''' % locals()
    P.run()


@merge(insertSizeMetricsAndHistograms,
       "qc.dir/qc_insert_size_metrics.load")
def loadInsertSizeMetrics(infiles, outfile):
    '''load the insert size metrics to a single table'''

    if PAIRED:
        picard_summaries = [x[0] for x in infiles]

        P.concatenateAndLoad(picard_summaries, outfile,
                             regex_filename=(".*/.*/(.*)"
                                             ".insert.size.metrics.summary"),
                             cat="cell",
                             options='')

    else:
        statement = '''echo "Not compatible with SE data"
                       > %(outfile)s
                    ''' % locals()
        P.run()


@merge(insertSizeMetricsAndHistograms,
       "qc.dir/qc_insert_size_histogram.load")
def loadInsertSizeHistograms(infiles, outfile):
    '''load the histograms to a single table'''

    if PAIRED:
        picard_histograms = [x[1] for x in infiles]

        P.concatenateAndLoad(picard_histograms, outfile,
                             regex_filename=(".*/.*/(.*)"
                                             ".insert.size.metrics.histogram"),
                             cat="cell",
                             options='-i "insert_size" -e')

    else:
        statement = '''echo "Not compatible with SE data"
                       > %(outfile)s
                    ''' % locals()
        P.run()


# -------------- No. reads mapping to spike-ins vs genome ------------------- #

@follows(mkdir("qc.dir/spike.vs.genome.dir"))
@transform(collectBAMs,
           regex(r".*/(.*).bam"),
           r"qc.dir/spike.vs.genome.dir/\1.uniq.mapped.reads")
def spikeVsGenome(infile, outfile):
    '''Summarise the number of reads mapping uniquely to spike-ins and genome.
       Compute the ratio of reads mapping to spike-ins vs genome.
       Only uniquely mapping reads are considered'''

    header = "\\t".join(["nreads_uniq_map_genome", "nreads_uniq_map_spike",
                        "fraction_spike"])

    statement = ''' echo -e "%(header)s" > %(outfile)s;
                    checkpoint;
                    samtools view %(infile)s
                    | grep NH:i:1
                    | awk 'BEGIN{OFS="\\t";ercc=0;genome=0};
                           $3~/chr*/{genome+=1};
                           $3~/ERCC*/{ercc+=1};
                           END{frac=ercc/(ercc+genome);
                               print genome,ercc,frac};'
                    >> %(outfile)s
                ''' % locals()
    P.run()


@merge(spikeVsGenome,
       "qc.dir/qc_spike_vs_genome.load")
def loadSpikeVsGenome(infiles, outfile):
    '''Load number of reads uniquely mapping to genome & spike-ins
       and fraction of spike-ins to a single db table'''

    P.concatenateAndLoad(infiles, outfile,
                         regex_filename=".*/.*/(.*).uniq.mapped.reads",
                         cat="cell",
                         options='-i "cell"')


# ------------------------- No. genes detected ------------------------------ #

@follows(mkdir("qc.dir/"))
@files(loadCopyNumber,
       "qc.dir/number.genes.detected")
def numberGenesDetected(infile, outfile):
    '''Count no genes detected at copynumer > 0 in each cell'''

    table = P.toTable(infile)

    sqlstat = '''select *
                 from %(table)s
                 where gene_id like "ENS%%"
              ''' % locals()

    df = DB.fetch_DataFrame(sqlstat, PARAMS["database_name"])
    df2 = df.pivot(index="gene_id", columns="track", values="copy_number")
    n_expressed = df2.apply(lambda x: np.sum([1 for y in x if y > 0]))

    n_expressed.to_csv(outfile, sep="\t")


@files(numberGenesDetected,
       "qc.dir/qc_no_genes_cufflinks.load")
def loadNumberGenesDetected(infile, outfile):
    '''load the numbers of genes expressed to the db'''

    P.load(infile, outfile,
           options='-i "cell" -H "cell,no_genes_cufflinks"')


# ------------------ No. genes detected htseq-count ---------------------- #


@files(loadHTSeqCounts,
       "qc.dir/number.genes.detected.htseq")
def numberGenesDetectedHTSeq(infile, outfile):
    '''Count no genes detected by htseq-count at counts > 0 in each cell'''

    table = P.toTable(infile)

    sqlstat = '''select *
                 from %(table)s
                 where gene_id like "ENS%%"
              ''' % locals()

    df = DB.fetch_DataFrame(sqlstat, PARAMS["database_name"])
    df2 = df.pivot(index="gene_id", columns="track", values="counts")
    n_expressed = df2.apply(lambda x: np.sum([1 for y in x if y > 0]))

    n_expressed.to_csv(outfile, sep="\t")


@files(numberGenesDetectedHTSeq,
       "qc.dir/qc_no_genes_htseq.load")
def loadNumberGenesDetectedHTSeq(infile, outfile):
    '''load the numbers of genes expressed to the db'''

    P.load(infile, outfile,
           options='-i "cell" -H "cell,no_genes_htseq"')


# --------------------- Fraction of spliced reads --------------------------- #

@follows(mkdir("qc.dir/fraction.spliced.dir/"))
@transform(collectBAMs,
           regex(r".*/(.*).bam"),
           r"qc.dir/fraction.spliced.dir/\1.fraction.spliced")
def fractionReadsSpliced(infile, outfile):
    '''Compute fraction of reads containing a splice junction.
       * paired-endedness is ignored
       * only uniquely mapping reads are considered'''

    statement = '''echo "fraction_spliced" > %(outfile)s;
                   checkpoint;
                   samtools view %(infile)s
                   | grep NH:i:1
                   | cut -f 6
                   | awk '{if(index($1,"N")==0){us+=1}
                           else{s+=1}}
                          END{print s/(us+s)}'
                   >> %(outfile)s
                 ''' % locals()

    P.run()


@merge(fractionReadsSpliced,
       "qc.dir/qc_fraction_spliced.load")
def loadFractionReadsSpliced(infiles, outfile):
    '''load to fractions of spliced reads to a single db table'''

    P.concatenateAndLoad(infiles, outfile,
                         regex_filename=".*/.*/(.*).fraction.spliced",
                         cat="cell",
                         options='-i "cell"')


# ---------------- Prepare a post-mapping QC summary ------------------------ #

@follows(mkdir("annotations.dir"))
@merge(collectBAMs,
       "annotations.dir/sample.information.txt")
def sampleInformation(infiles, outfile):
    '''make a database table containing per-cell sample information.'''

    name_field_list = PARAMS["name_field_titles"]
    name_fields = name_field_list.strip().split(",")
    header = ["\t".join(["cell"] + name_fields + ["mapper"])]

    sep = PARAMS["name_field_separator"]

    contents = []
    for infile in infiles:

        cell = os.path.basename(infile)[:-len(".bam")]

        if "." in cell:
            cell_name_fields, mapper = cell.split(".", 1)
        else:
            cell_name_fields, mapper = cell, "unknown"

        contents.append("\t".join([cell_name_fields] +
                                  cell_name_fields.split(sep) +
                                  [mapper]))

    with open(outfile, "w") as of:
        of.write("\n".join(header + contents))


@transform(sampleInformation,
           suffix(".txt"),
           ".load")
def loadSampleInformation(infile, outfile):
    '''load the sample information table to the db'''

    P.load(infile, outfile)


@merge([loadSampleInformation,
        loadCollectRnaSeqMetrics,
        loadThreePrimeBias,
        loadEstimateLibraryComplexity,
        loadSpikeVsGenome,
        loadFractionReadsSpliced,
        loadNumberGenesDetected,
        loadNumberGenesDetectedHTSeq,
        loadAlignmentSummaryMetrics,
        loadInsertSizeMetrics],
       "qc.dir/qc_summary.txt")
def qcSummary(infiles, outfile):
    '''create a summary table of relevant QC metrics'''

    # Some QC metrics are specific to paired end data
    if PAIRED:
        exclude = []
        paired_columns = '''READ_PAIRS_EXAMINED as no_pairs,
                              PERCENT_DUPLICATION as pct_duplication,
                              ESTIMATED_LIBRARY_SIZE as library_size,
                              PCT_READS_ALIGNED_IN_PAIRS
                                       as pct_reads_aligned_in_pairs,
                              MEDIAN_INSERT_SIZE
                                       as median_insert_size,
                           '''
        pcat = "PAIR"

    else:
        exclude = ["qc_library_complexity", "qc_insert_size_metrics"]
        paired_columns = ''
        pcat = "UNPAIRED"

    tables = [P.toTable(x) for x in infiles
              if P.toTable(x) not in exclude]

    t1 = tables[0]

    name_fields = PARAMS["name_field_titles"].strip()

    stat_start = '''select distinct %(name_fields)s,
                                    mapper,
                                    %(t1)s.cell,
                                    fraction_spliced,
                                    fraction_spike,
                                    no_genes_cufflinks,
                                    no_genes_htseq,
                                    three_prime_bias
                                       as three_prime_bias,
                                    nreads_uniq_map_genome,
                                    nreads_uniq_map_spike,
                                    %(paired_columns)s
                                    PCT_MRNA_BASES
                                       as pct_mrna,
                                    PCT_CODING_BASES
                                       as pct_coding,
                                    PCT_PF_READS_ALIGNED
                                       as pct_reads_aligned,
                                    TOTAL_READS
                                       as total_reads,
                                    PCT_ADAPTER
                                       as pct_adapter,
                                    PF_HQ_ALIGNED_READS*1.0/PF_READS
                                       as pct_pf_reads_aligned_hq
                   from %(t1)s
                ''' % locals()

    join_stat = ""
    for table in tables[1:]:
        join_stat += "left join " + table + "\n"
        join_stat += "on " + t1 + ".cell=" + table + ".cell\n"

    where_stat = '''where qc_alignment_summary_metrics.CATEGORY="%(pcat)s"
                 ''' % locals()

    statement = "\n".join([stat_start, join_stat, where_stat])

    df = DB.fetch_DataFrame(statement, PARAMS["database_name"])
    df.to_csv(outfile, sep="\t", index=False)


@transform(qcSummary,
           suffix(".txt"),
           ".load")
def loadQCSummary(infile, outfile):
    '''load summary to db'''

    P.load(infile, outfile)


@follows(loadQCSummary, loadInsertSizeHistograms)
def qc():
    '''target for executing qc'''
    pass


# --------------------- < generic pipeline tasks > -------------------------- #

@follows(mkdir("notebook.dir"))
@transform(glob.glob(os.path.join(os.path.dirname(__file__),
                                  "pipeline_notebooks",
                                  os.path.basename(__file__)[:-len(".py")],
                                  "*")),
           regex(r".*/(.*)"),
           r"notebook.dir/\1")
def notebooks(infile, outfile):
    '''Utility function to copy the notebooks from the source directory
       to the working directory'''

    shutil.copy(infile, outfile)


@follows(quantitation, qc, notebooks)
def full():
    pass

if __name__ == "__main__":
    sys.exit(P.main(sys.argv))
