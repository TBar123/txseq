"""==================
Pipeline salmon.py
==================

Overview
--------

This pipeline quantifies gene expression from FASTQ files using `Salmon <https://github.com/COMBINE-lab/salmon>`_. 


Configuration
-------------

The pipeline requires a configured :file:`pipeline_salmon.yml` file.

A default configuration file can be generated by executing: ::

   txseq salmon config


Inputs
------

The pipeline requires the following inputs

#. samples.tsv: see :doc:`Configuration files<configuration>`
#. libraries.tsv: see :doc: `Configuration files<configuration>`
#. txseq annotations: the location where the :doc:`pipeline_ensembl.py </pipelines/pipeline_ensembl>` was run to prepare the annotatations.
#. Salmon index: the location of a salmon index built with :doc:`pipeline_salmon_index.py </pipelines/pipeline_salmon_index>`.


Requirements
------------

The following software is required:

#. Salmon

Output files
------------

The pipeline produces the following outputs:

#. per-sample salmon quantification results in the "salmon.dir" folder
#. a csvdb sqlite database that contains tables of gene and transcript counts and TPMs

.. note::

    It is strongly recommended to parse the raw Salmon results using the `tximport <https://bioconductor.org/packages/release/bioc/html/tximport.html>`_ Bioconductor R package for downstream analysis.
    

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
PARAMS["txseq_code_dir"] = Path(__file__).parents[1]


if len(sys.argv) > 1:
    if(sys.argv[1] == "make"):
        
        # set the location of the code directory 
        S = T.samples(sample_tsv = PARAMS["samples"],
                            library_tsv = PARAMS["libraries"])
        
        # Set the database location
        DATABASE = PARAMS["sqlite"]["file"]


# ---------------------- < specific pipeline tasks > ------------------------ #
    
# ---------------------- Salmon TPM calculation ----------------------------- #

def salmon_jobs():

    for sample_id in S.samples.keys():
    
        yield([None,
               os.path.join("salmon.dir",
                            sample_id + ".sentinel"
        )])

@files(salmon_jobs)
def quant(infile, outfile):
    '''
    Per sample quantitation using salmon.
    '''
    
    t = T.setup(infile, outfile, PARAMS,
                memory=PARAMS["salmon_memory"],
                cpu=PARAMS["salmon_threads"])

    sample = S.samples[os.path.basename(outfile)[:-len(".sentinel")]]

    if sample.paired:
        fastq_input = "-1 " + " ".join(sample.fastq["read1"]) +\
                      " -2 " + " ".join(sample.fastq["read2"])

    else:
        fastq_input = "-r " + " ".join(sample.fastq["read1"])

    options = ''
    if not PARAMS['salmon_quant_options'] is None:
        options = PARAMS['salmon_quant_options']
    
    libtype = sample.salmon_libtype
    
    out_path = os.path.join(t.outdir, sample.sample_id)

    tx2gene = os.path.join(PARAMS["txseq_annotations"],"api.dir/txseq.transcript.to.gene.map")

    statement = '''salmon quant -i %(txseq_salmon_index)s
                                -p %(job_threads)s
                                -g %(tx2gene)s
                                %(options)s
                                -l %(libtype)s
                                %(fastq_input)s
                                -o %(out_path)s
                    &> %(log_file)s;
              ''' % dict(PARAMS, **t.var, **locals())
              
    P.run(statement, **t.resources)
    
    IOTools.touch_file(outfile)

@merge(quant, 
       "salmon.dir/salmon.transcripts.sentinel")
def loadSalmonTranscriptQuant(infiles, sentinel):
    '''
    Load the salmon transcript-level results.
    '''

    tables = [x.replace(".sentinel", "/quant.sf") for x in infiles]

    outfile = sentinel.replace(".sentinel",".load")

    P.concatenate_and_load(tables, outfile,
                           regex_filename=".*/(.*)/quant.sf",
                           cat="sample_id",
                           options="-i Name -i sample_id",
                           job_memory=PARAMS["sql_himem"])
    
    IOTools.touch_file(sentinel)

@follows(loadSalmonTranscriptQuant)
@merge(quant, "salmon.dir/salmon.genes.sentinel")
def loadSalmonGeneQuant(infiles, sentinel):
    '''
    Load the salmon gene-level results.
    '''

    tables = [x.replace(".sentinel", "/quant.genes.sf") for x in infiles]
    outfile = sentinel.replace(".sentinel",".load")

    P.concatenate_and_load(tables, outfile,
                           regex_filename=".*/(.*)/quant.genes.sf",
                           cat="sample_id",
                           options="-i Name -i sample_id",
                           job_memory=PARAMS["sql_himem"])
    
    IOTools.touch_file(sentinel)


@jobs_limit(1)
@transform([loadSalmonTranscriptQuant,
            loadSalmonGeneQuant],
           regex(r"(.*)/(.*).sentinel"),
           r"\1/\2.tpms.sentinel")
def salmonTPMs(infile, outfile):
    '''
    Prepare a wide table of salmon TPMs (samples x transcripts|genes).
    '''

    t = T.setup(infile, outfile, PARAMS,
                memory="24G",
                cpu=1)

    table = P.to_table(infile.replace(".sentinel",".load"))
    database = DATABASE

    if "transcript" in table:
        id_name = "transcript_id"
    elif "gene" in table:
        id_name = "gene_id"
    else:
        raise ValueError("Unexpected Salmon table name")

    statement = '''python %(txseq_code_dir)s/python/salmon_fetch_tpms.py
                   --database=%(database)s
                   --table=%(table)s
                   --idname=%(id_name)s
                   --outfile=%(out_file)s.txt.gz
                   &> %(log_file)s
                ''' % dict(PARAMS, **t.var, **locals())
              
    P.run(statement, **t.resources)
    
    IOTools.touch_file(outfile)


@follows(loadSalmonGeneQuant)
@jobs_limit(1)
@transform(salmonTPMs,
           suffix(".sentinel"),
           ".load")
def loadSalmonTPMs(infile, outfile):
    '''
    Load a wide table of salmon TPMs in the project database.
    '''

    if "transcript" in infile:
        id_name = "transcript_id"
    elif "gene" in infile:
        id_name = "gene_id"
    else:
        raise ValueError("Unexpected Salmon table name")

    opts = "-i " + id_name

    file_name = infile[:-len(".sentinel")] + ".txt.gz"

    P.load(file_name, outfile, options=opts,
           job_memory=PARAMS["sql_himem"])


@follows(quant)
@files(None,"tximeta.dir/tximeta.sentinel")
def tximeta(infile, outfile):
    '''
    Run tximeta to summarise counts and gene and transcript level.
    '''
    
    t = T.setup(infile, outfile, PARAMS,
                memory="24G")
    
    geneset_path = os.path.join(PARAMS["txseq_annotations"],
                                "api.dir/txseq.geneset.gtf.gz")
    transcript_path = os.path.join(PARAMS["txseq_annotations"],
                                   "api.dir/txseq.transcript.fa.gz")
    
    statement = '''Rscript %(txseq_code_dir)s/R/scripts/tximeta.R
                   --indexdir=%(txseq_salmon_index)s
                   --salmondir=salmon.dir
                   --transcripts=%(transcript_path)s 
                   --geneset=%(geneset_path)s
                   --organism="%(tximeta_organism)s"
                   --genomeversion=%(tximeta_genome)s
                   --release=%(tximeta_release)s
                   --samples=%(samples)s
                   --outfile=%(out_file)s.RDS
                   &> %(log_file)s
    ''' % dict(PARAMS, **t.var, **locals())
              
    P.run(statement, **t.resources)
    
    IOTools.touch_file(outfile)



# ----------------------- Quantitation target ------------------------------ #

@follows(loadSalmonTPMs) #, loadCopyNumber)
def quantitation():
    '''
    Quantitation target.
    '''
    pass


# ----------------------- load txinfo ------------------------------ #

@follows(loadSalmonTPMs)
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


# ------------------------- No. genes detected ------------------------------ #

@jobs_limit(1)
@follows(mkdir("qc.dir/"), loadSalmonTPMs, loadTranscriptInfo)
@files("salmon.dir/salmon.genes.tpms.load",
       "qc.dir/number.genes.detected.salmon.sentinel")
def numberGenesDetected(infile, outfile):
    '''
    Count no genes detected at copynumer > 0 in each sample.
    '''

    t = T.setup(infile, outfile, PARAMS,
            memory="24G",
            cpu=1)

    table = P.to_table(infile)

    database = DATABASE

    statement = '''python %(txseq_code_dir)s/python/salmon_no_genes_detected.py
                   --database=%(database)s
                   --table=%(table)s
                   --outfile=%(out_file)s.tsv.gz
                   &> %(log_file)s
                ''' % dict(PARAMS, **t.var, **locals())
              
    P.run(statement, **t.resources)
    
    IOTools.touch_file(outfile)

@follows(loadTranscriptInfo)
@jobs_limit(1)
@files(numberGenesDetected,
       "qc.dir/qc_no_genes_salmon.load")
def loadNumberGenesDetected(infile, outfile):
    '''
    Load the numbers of genes expressed to the project database.
    '''
    
    P.load(infile[:-len(".sentinel")] + ".tsv.gz", 
           outfile,
           options='-i "sample_id"')


# --------------------- < generic pipeline tasks > -------------------------- #

@follows(quantitation, 
         tximeta,
         loadNumberGenesDetected)
def full():
    pass


def main(argv=None):
    if argv is None:
        argv = sys.argv
    P.main(argv)

if __name__ == "__main__":
    sys.exit(P.main(sys.argv))
