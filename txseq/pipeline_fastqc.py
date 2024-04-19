"""==================
Pipeline fastqc.py
==================

:Author: Stephen Sansom
:Release: $Id$
:Date: |today|
:Tags: Python


Overview
--------

The pipeline runs the `FASTQC quality control tool <https://www.bioinformatics.babraham.ac.uk/projects/fastqc/>`_ and post-processes the output for downstream-visualisation.


Configuration
-------------

The pipeline requires a configured :file:`pipeline_fastqc.yml` file.

A default configuration file can be generated by executing: ::

   txseq fastqc config


Inputs
------

The pipeline requires the following inputs

#. samples.tsv: see :doc:`Configuration files<configuration>`
#. libraries.tsv: see :doc: `Configuration files<configuration>`

Requirements
------------

The following software is required:

#. FastQC

Output files
------------

The pipeline produces the following outputs:

#. fastqc results: for each FASTQ file in the "fastqc.dir" sub-folder
#. An sqlite database: in a file named "csvdb" which contain summary tables of the fastqc results e.g. for plotting in R.

.. note::
    For quick visualations of the fastqc results it is recommended to use `MultiQC <https://multiqc.info>`_.



Code
====

"""
from ruffus import *

import sys
import shutil
import os
import re
from pathlib import Path
import glob
import sqlite3

import pandas as pd
import numpy as np

import sqlalchemy
from sqlalchemy import text

from cgatcore import experiment as E
from cgatcore import pipeline as P
import cgatcore.iotools as IOTools


# import local pipeline utility functions
import txseq.tasks as T
import txseq.tasks.readqc as readqc

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
                      library_tsv = PARAMS["libraries"])

# ########################################################################### #
# ############################ Run FASTQC  ################################## #
# ########################################################################### #

def fastq_jobs():

    for seq_id in S.fastqs.keys():
    
        yield([None, os.path.join("fastqc.dir",
                                  seq_id + ".sentinel")])


@files(fastq_jobs)
def fastqc(infile, outfile):
    
    t = T.setup(infile, outfile, PARAMS)

    seq_id = os.path.basename(outfile[:-len(".sentinel")])

    out_path = os.path.join(t.outdir, seq_id)
    
    if not os.path.exists(out_path):
        os.makedirs(out_path)

    fastq_path = S.fastqs[seq_id]['fastq_path']
    
    contaminants = ""
    if PARAMS["fastqc_contaminants"] != "default":
        contaminants = "--contaminants " + PARAMS["fastqc_contaminants"]
    
    adaptors = ""
    if PARAMS["fastqc_adaptors"] != "default":
        adaptors = "-a " + PARAMS["fastqc_adaptors"]
        
    limits = ""
    if PARAMS["fastqc_limits"] != "default":
        limits = "-l " + PARAMS["fastqc_limits"]

    statement = '''fastqc 
                   -o %(out_path)s
                   --extract
                   %(contaminants)s
                   %(adaptors)s
                   %(limits)s
                   %(fastq_path)s
                   > %(log_file)s
                ''' % dict(PARAMS, **t.var, **locals())
                
    P.run(statement)
    IOTools.touch_file(outfile)


@split(fastqc, ["fastqc.summary.dir/fastqc_basic_statistics.tsv.gz", 
                "fastqc.summary.dir/fastqc_*.tsv.gz"])
def summarizeFastQC(infiles, outfiles):

    t = T.setup(infiles[0], outfiles[0], PARAMS)

    all_files = []

    for infile in infiles:

        track = P.snip(infile, ".sentinel")
        all_files.extend(glob.glob(
            os.path.join(track, "*_fastqc",
                         "fastqc_data.txt")))

    dfs = readqc.read_fastqc(
        all_files)

    for key, df in dfs.items():
        fn = re.sub("basic_statistics", key, outfiles[0])
        E.info("writing to {}".format(fn))
        with IOTools.open_file(fn, "w") as outf:
            df.to_csv(outf, sep="\t", index=True)


@merge(fastqc, 
       "fastqc.summary.dir/fastqc_status_summary.tsv.gz")
def buildFastQCSummaryStatus(infiles, outfile):
    '''load FastQC status summaries into a single table.'''
    readqc.buildFastQCSummaryStatus(
        infiles,
        outfile,
        "fastqc.dir")


@jobs_limit(1) #P.get_params().get("jobs_limit_db", 1), "db")
@transform((summarizeFastQC, buildFastQCSummaryStatus),
           suffix(".tsv.gz"), ".load")
def loadFastQC(infile, outfile):
    '''load FASTQC stats into database.'''

    # a check to make sure file isn't empty
    n = 0
    with IOTools.open_file(infile) as f:
        for i, line in enumerate(f):
            n =+ i
    if n > 0:
        P.load(infile, outfile, options="--add-index=track")
    else:
        table_name = os.path.basename(infile).replace(".tsv.gz", "")
        database_sql = P.get_params()["database"]["url"]
        database_name = os.path.basename(database_sql)
        statement = """sqlite3 %(database_name)s
                       'DROP TABLE IF EXISTS %(table_name)s;
                       CREATE TABLE %(table_name)s
                       ("track" text PRIMARY KEY, "Sequence" text,
                       "Count" integer, "Percentage" integer, "Possible Source" text);'
                       'INSERT INTO %(table_name)s VALUES ("NA", "NA", 0, 0, "NA");'"""

        P.run(statement)

@files(None, "fastqc.dir/load.metadata.sentinel")
def loadMetadata(infile, outfile):
    '''load the sample and fastq table into the database'''
    
    db =sqlalchemy.create_engine('sqlite:///' + PARAMS["sqlite_file"])

    with db.connect() as dbconn:

        S.sample_table.to_sql(name = 'samples',
                              con= dbconn, 
                              index= False, 
                              if_exists='replace') 
    
    

        dbconn.execute(text("CREATE INDEX samples_sample_id on samples (sample_id)"))
        
    with db.connect() as dbconn:
    
        S.fastq_table.to_sql(name = 'fastqs',
                            con= dbconn, 
                            index=False, 
                            if_exists='replace') 

        dbconn.execute(text("CREATE INDEX fastqs_fastq_id on fastqs (sample_id)"))
    
    db.dispose()

    IOTools.touch_file(outfile)

# --------------------- < generic pipeline tasks > -------------------------- #


@follows(fastqc, loadFastQC, loadMetadata)
def full():
    pass


def main(argv=None):
    if argv is None:
        argv = sys.argv
    P.main(argv)

if __name__ == "__main__":
    sys.exit(P.main(sys.argv))