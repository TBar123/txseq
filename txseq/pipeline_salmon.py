"""===============
Pipeline Salmon
===============

Overview
========

This pipeline quantifies gene expression from FASTQ files using `Salmon <https://github.com/COMBINE-lab/salmon>`_. 


Usage
=====

See :ref:`PipelineSettingUp` and :ref:`PipelineRunning` on general
information how to use CGAT pipelines.

Configuration
-------------

The pipeline requires a configured :file:`pipeline_sample.yml` file.

Default configuration files can be generated by executing:

   python <srcdir>/pipeline_salmon.py config


Inputs
------

1. Salmon index
^^^^^^^^^^^^^^^

 A prebuilt salmon index, which can be built using :doc:`pipeline_salmon_index.py </pipelines/pipeline_salmon_index>`.

2. Transcript GTF
^^^^^^^^^^^^^^^^^

Used to map transcripts to genes for gene level quantification. 


The location of these files must be specified in the 'pipeline_salmon.yml' file.

Use of spike ins
----------------

TODO: add support for use of spike ins!

If spike-ins are used, the location of a table containing the per-cell
spike in copy numbers should be provided in a file specified in pipeline_salmon.yml

The expected structure (tab-delimited) is:

    gene_id|copies_per_cell
    ERCC-..|20324.7225

Note that the columns headers "gene_id" and "copies_per_cell" are required.


Requirements
------------

On top of the default CGAT setup, the pipeline requires the following
software to be in the path:

.. Add any additional external requirements such as 3rd party software
   or R modules below:

Requirements (TBC):

* Salmon

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
import txseq.tasks.samples as samples

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
        S = samples.samples(sample_tsv = PARAMS["sample_table"],
                            library_tsv = PARAMS["library_table"])


# Set the database locations
DATABASE = PARAMS["database"]["file"]
ANN_DATABASE = PARAMS["annotations_database"]


# ---------------------- < specific pipeline tasks > ------------------------ #
    
# ########################################################################### #
# ################ (2) Quantitation of gene expression #################### #
# ########################################################################### #

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
                memory=PARAMS["quant_resources_memory"],
                cpu=PARAMS["quant_resources_threads"])

    sample = S.samples[os.path.basename(outfile)[:-len(".sentinel")]]

    if sample.paired:
        fastq_input = "-1 " + " ".join(sample.fastq["read1"]) +\
                      " -2 " + " ".join(sample.fastq["read2"])

    else:
        fastq_input = "-r " + " ".join(sample.fastq["read1"])

    options = ''
    if not PARAMS['quant_options'] is None:
        options = PARAMS['quant_options']
    
    libtype = sample.salmon_libtype
    
    out_path = os.path.join(t.outdir, sample.sample_id)

    statement = '''salmon quant -i %(quant_index)s
                                -p %(job_threads)s
                                -g %(quant_gtf)s
                                %(options)s
                                -l %(libtype)s
                                %(fastq_input)s
                                -o %(out_path)s
                    &> %(log_file)s;
              ''' % dict(PARAMS, **t.var, **locals())
              
    P.run(statement)
    
    IOTools.touch_file(outfile)

@merge(quant, "salmon.dir/salmon.transcripts.load")
def loadSalmonTranscriptQuant(infiles, outfile):
    '''
    Load the salmon transcript-level results.
    '''

    tables = [x.replace(".log", "/quant.sf") for x in infiles]

    P.concatenate_and_load(tables, outfile,
                           regex_filename=".*/(.*)/quant.sf",
                           cat="sample_id",
                           options="-i Name -i sample_id",
                           job_memory=PARAMS["sql_himem"])


@merge(quant, "salmon.dir/salmon.genes.load")
def loadSalmonGeneQuant(infiles, outfile):
    '''
    Load the salmon gene-level results.
    '''

    tables = [x.replace(".log", "/quant.genes.sf") for x in infiles]

    P.concatenate_and_load(tables, outfile,
                           regex_filename=".*/(.*)/quant.genes.sf",
                           cat="sample_id",
                           options="-i Name -i sample_id",
                           job_memory=PARAMS["sql_himem"])


@jobs_limit(1)
@transform([loadSalmonTranscriptQuant,
            loadSalmonGeneQuant],
           regex(r"(.*)/(.*).load"),
           r"\1/\2.tpms.txt")
def salmonTPMs(infile, outfile):
    '''
    Prepare a wide table of salmon TPMs (samples x transcripts|genes).
    '''

    table = P.to_table(infile)

    if "transcript" in table:
        id_name = "transcript_id"
    elif "gene" in table:
        id_name = "gene_id"
    else:
        raise ValueError("Unexpected Salmon table name")

    con = sqlite3.connect(PARAMS["database_file"])
    c = con.cursor()

    sql = '''select sample_id, Name %(id_name)s, TPM tpm
             from %(table)s
          ''' % locals()

    df = pd.read_sql(sql, con)

    df = df.pivot(id_name, "sample_id", "tpm")
    df.to_csv(outfile, sep="\t", index=True, index_label=id_name)


@transform(salmonTPMs,
           suffix(".txt"),
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

    P.load(infile, outfile, options=opts,
           job_memory=PARAMS["sql_himem"])





# ---------------------- Copynumber estimation ------------------------------ #

# Copy number estimation based on spike-in sequences and Salmon TPMs.
if PARAMS["spikein_estimate_copy_numbers"] is True:
    run_copy_number_estimation = True
else:
    run_copy_number_estimation = False


@active_if(run_copy_number_estimation)
@follows(mkdir("copy.number.dir"), loadSalmonTPMs)
@files("salmon.dir/salmon.genes.tpms.txt",
       "copy.number.dir/copy_numbers.txt")
def estimateCopyNumber(infile, outfile):
    '''
    Estimate copy numbers based on standard
    curves constructed from the spike-ins.
    '''

    statement = '''Rscript %(scseq_dir)s/R/calculate_copy_number.R
                   --spikeintable=%(spikein_copy_numbers)s
                   --spikeidcolumn=gene_id
                   --spikecopynumbercolumn=copies_per_cell
                   --exprstable=%(infile)s
                   --exprsidcolumn=gene_id
                   --outfile=%(outfile)s
                '''
    P.run(statement)


@active_if(run_copy_number_estimation)
@transform(estimateCopyNumber,
           suffix(".txt"),
           ".load")
def loadCopyNumber(infile, outfile):
    '''
    Load the copy number estimations to the project database.
    '''

    P.load(infile, outfile, options='-i "gene_id"')


# ----------------------- Quantitation target ------------------------------ #

@follows(loadSalmonTPMs, loadCopyNumber)
def quantitation():
    '''
    Quantitation target.
    '''
    pass



# ------------------------- No. genes detected ------------------------------ #

@follows(mkdir("qc.dir/"), loadSalmonTPMs)
@files("salmon.dir/salmon.genes.tpms.load",
       "qc.dir/number.genes.detected.salmon")
def numberGenesDetectedSalmon(infile, outfile):
    '''
    Count no genes detected at copynumer > 0 in each sample.
    '''

    table = P.to_table(infile)

    statement = '''select distinct s.*, i.gene_biotype
                   from %(table)s s
                   inner join transcript_info i
                   on s.gene_id=i.gene_id
                ''' % locals()

    df = DB.fetch_DataFrame(statement, DATABASE)

    melted_df = pd.melt(df, id_vars=["gene_id", "gene_biotype"])

    grouped_df = melted_df.groupby(["gene_biotype", "variable"])

    agg_df = grouped_df.agg({"value": lambda x:
                             np.sum([1 for y in x if y > 0])})
    agg_df.reset_index(inplace=True)

    count_df = pd.pivot_table(agg_df, index="variable",
                              values="value", columns="gene_biotype")
    count_df["total"] = count_df.apply(np.sum, 1)
    count_df["sample_id"] = count_df.index

    count_df.to_csv(outfile, index=False, sep="\t")

@files(numberGenesDetectedSalmon,
       "qc.dir/qc_no_genes_salmon.load")
def loadNumberGenesDetectedSalmon(infile, outfile):
    '''
    Load the numbers of genes expressed to the project database.
    '''

    P.load(infile, outfile,
           options='-i "sample_id"')


# --------------------- < generic pipeline tasks > -------------------------- #

@follows(quant)
def full():
    pass


def main(argv=None):
    if argv is None:
        argv = sys.argv
    P.main(argv)

if __name__ == "__main__":
    sys.exit(P.main(sys.argv))
