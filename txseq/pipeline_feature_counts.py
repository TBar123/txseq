"""=========================
Pipeline feature_counts.py
=========================

Overview
--------

This pipeline counts the number of reads mapped to transcript/gene models. It uses the featureCounts algorithm from the `Subread package <https://subread.sourceforge.net>`_ .


Configuration
-------------

The pipeline requires a configured :file:`pipeline_feature_counts.yml` file.

A default configuration file can be generated by executing: ::

   txseq salmon feature_counts


Inputs
------

The pipeline requires the following inputs

#. samples.tsv: see :doc:`Configuration files<configuration>`
#. txseq annotations: the location where the :doc:`pipeline_ensembl.py </pipelines/pipeline_ensembl>` was run to prepare the annotatations.
#. bam files: the location of a folder containing the bam files named by "sample_id".


Requirements
------------

The following software is required:

#. Subread


Output files
------------

The pipeline produces the following outputs:

#. per-sample results: in the "feature.counts.dir" subdirectory
#. An sqlite database: in a file named "csvdb" which contains the per-gene counts.


Code
====

"""
from ruffus import *

import sys
import shutil
import os
from pathlib import Path
import glob
import sqlite3

import pandas as pd
import numpy as np

from cgatcore import experiment as E
from cgatcore import pipeline as P
from cgatcore import database as DB
import cgatcore.iotools as IOTools


# import local pipeline utility functions
import txseq.tasks as T

# ----------------------- < pipeline configuration > ------------------------ #

# Override function to collect config files
P.control.write_config_files = T.write_config_files

# load options from the yml file
P.parameters.HAVE_INITIALIZED = False
PARAMS = P.get_parameters(T.get_parameter_file(__file__))

# set the location of the code directory
PARAMS["txseq_code_dir"] = Path(__file__).parents[1]

if len(sys.argv) > 1:
    if(sys.argv[1] == "make"):
        S = T.samples(sample_tsv = PARAMS["samples"],
                      library_tsv = None)
        
        # Set the database location
        DATABASE = PARAMS["sqlite"]["file"]


# ---------------------- < specific pipeline tasks > ------------------------ #

# ----------------------------- Read Counting ------------------------------- #

def count_jobs():

    for sample_id in S.samples.keys():
    
        yield([os.path.join(PARAMS["bam_path"], sample_id + ".bam"),
                os.path.join("feature.counts.dir/",
                            sample_id + ".counts.sentinel")])

@files(count_jobs)
def count(infile, sentinel):
    '''
    Run featureCounts.
    '''

    t = T.setup(infile, sentinel, PARAMS,
            cpu=PARAMS["featurecounts_threads"])

    sample_id = os.path.basename(infile)[:-len(".bam")]
    sample = S.samples[sample_id]

    # set featureCounts options
    featurecounts_strand = sample.featurecounts_strand

    if sample.paired:
        paired_options = "-p"
    else:
        paired_options = ""

    if PARAMS["featurecounts_options"] is None:
        featurecounts_options = ""
    else:
        featurecounts_options = PARAMS["featurecounts_options"]

    mktemp_template = "ctmp.featureCounts.XXXXXXXXXX"
    counts_file = sentinel.replace(".sentinel", ".gz")
    summary_file = sentinel.replace(".sentinel", ".summary")

    geneset = os.path.join(PARAMS["txseq_annotations"],
                           "api.dir/txseq.geneset.gtf.gz")

    statement = '''counts=`mktemp -p . %(mktemp_template)s`;
                   featureCounts
                    -a %(geneset)s
                    -o $counts
                    -s %(featurecounts_strand)s
                    -T %(featurecounts_threads)s
                    %(featurecounts_options)s
                    %(paired_options)s
                    %(infile)s;
                    cut -f1,7 $counts
                    | grep -v "#" | grep -v "Geneid"
                    | gzip -c > %(counts_file)s;
                    rm $counts;
                    mv ${counts}.summary %(summary_file)s;
                 ''' % dict(PARAMS, **t.var, **locals())

    P.run(statement)
    IOTools.touch_file(sentinel)


@merge(count,
       "feature.counts.dir/featurecounts.load")
def loadCounts(infiles, outfile):
    '''
    Combine and load count data in the project database.
    '''
    
    infiles = [x.replace(".sentinel", ".gz") for x in infiles]

    P.concatenate_and_load(infiles, outfile,
                           regex_filename=".*/(.*).counts.gz",
                           has_titles=False,
                           cat="track",
                           header="track,gene_id,counts",
                           options='-i "gene_id"',
                           job_memory=PARAMS["sql_himem"])


@files(loadCounts,
       "feature.counts.dir/featurecounts_counts.sentinel")
def geneCounts(infile, outfile):
    '''
    Prepare a gene-by-sample table of featureCounts counts.
    '''

    t = T.setup(infile, outfile, PARAMS,
                memory="24G",
                cpu=1)
    table = P.to_table(infile)

    database = DATABASE

    statement = '''python %(txseq_code_dir)s/python/feature_counts_table.py
                   --database=%(database)s
                   --table=%(table)s
                   --outfile=%(out_file)s.tsv.gz
                   &> %(log_file)s
                ''' % dict(PARAMS, **t.var, **locals())
              
    P.run(statement, **t.resources)
    
    IOTools.touch_file(outfile)




@transform(geneCounts,
           suffix(".sentinel"),
           ".load")
def loadGeneCounts(infile, outfile):
    '''
    Load the gene-by-sample matrix of count data in the project database.
    '''

    P.load(infile[:-len(".sentinel")]+".tsv.gz", 
           outfile, options='-i "gene_id"')


# ----------------------- load txinfo ------------------------------ #

@files(None,
       "transcript.info.load")
def loadTranscriptInfo(infile, outfile):
    '''
    Load the annotations for salmon into the project database.
    '''

    txinfo = os.path.join(PARAMS["txseq_annotations"],
                          "api.dir/txseq.transcript.info.tsv.gz")
    
    if not os.path.exists(txinfo):
        raise ValueError("txseq annotations transcript information file not found")

    # will use ~15G RAM
    P.load(txinfo, outfile, options='-i "gene_id" -i "transcript_id"')
    

@follows(loadTranscriptInfo)
@files(loadCounts,
       "feature.counts.dir/number.genes.detected.featurecounts.sentinel")
def nGenesDetected(infile, outfile):
    '''
    Count of genes detected by featureCount at counts > 0 in each sample.
    '''

    t = T.setup(infile, outfile, PARAMS,
            memory="24G",
            cpu=1)

    table = P.to_table(infile)

    database = DATABASE

    statement = '''python %(txseq_code_dir)s/python/feature_counts_no_genes_detected.py
                   --database=%(database)s
                   --table=%(table)s
                   --outfile=%(out_file)s.tsv.gz
                   &> %(log_file)s
                ''' % dict(PARAMS, **t.var, **locals())
              
    P.run(statement, **t.resources)
    
    IOTools.touch_file(outfile)


@files(nGenesDetected,
       "feature.counts.dir/qc_no_genes_featurecounts.load")
def loadNGenesDetected(infile, outfile):
    '''
    Load the numbers of genes expressed to the project database.
    '''

    P.load(infile[:-len(".sentinel")] + ".tsv.gz", 
           outfile,
           options='-i "sample_id"')


# --------------------- < generic pipeline tasks > -------------------------- #


@follows(loadNGenesDetected, loadGeneCounts)
def full():
    pass


print(sys.argv)

def main(argv=None):
    if argv is None:
        argv = sys.argv
    P.main(argv)

if __name__ == "__main__":
    sys.exit(P.main(sys.argv))
