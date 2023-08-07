"""===============
Pipeline hisat
===============

Overview
========

This pipeline quantifies gene expression from FASTQ files using `Hisat2 <http://daehwankimlab.github.io/hisat2/>`_. 


Usage
=====

See :ref:`PipelineSettingUp` and :ref:`PipelineRunning` on general
information how to use CGAT pipelines.

Configuration
-------------

The pipeline requires a configured :file:`pipeline_hisat.yml` file.

Default configuration files can be generated by executing:

   python <srcdir>/pipeline_hisat.py config


Inputs
------

1. Hisat2 index
^^^^^^^^^^^^^^^

 A prebuilt hisat2 index, which can be built using :doc:`pipeline_hisat_index.py </pipelines/pipeline_hisat_index>`.

2. samples.tsv
^^^^^^^^^^^^^^
For details of this file see :doc:`pipeline_setup.py </pipelines/pipeline_setup>`

3. libraries.tsv
^^^^^^^^^^^^^^^^
For details of this file see :doc:`pipeline_setup.py </pipelines/pipeline_setup>`

The location of these files must be specified in the 'pipeline_salmon.yml' file.

Requirements
------------

On top of the default CGAT setup, the pipeline requires the following
software to be in the path:

Requirements:

* Hisat2

Pipeline output
===============

The pipeline produces sorted, indexed BAM files named by "sample_id" that are linked into the "api/hisat2" directory.

To register the BAMs for use in downstream pipelines, run the command: ::

    txseq hisat useBams

.. note:: The 'useBams' command is not run by default (i.e. it is not in the 'make full' chain).

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


# ---------------------- < specific pipeline tasks > ------------------------ #
    
# ########################################################################### #
# ################ (1) Quantitation of gene expression #################### #
# ########################################################################### #


def hisat_first_pass_jobs():

    for sample_id in S.samples.keys():
    
        yield([None,
               os.path.join("hisat.dir", "first.pass.dir",
                            sample_id + ".sentinel"
        )])

@files(hisat_first_pass_jobs)
def firstPass(infile, sentinel):
    '''
    Run a first hisat pass to identify novel splice sites.
    '''

    t = T.setup(infile, sentinel, PARAMS,
                memory=PARAMS["align_resources_memory"],
                cpu=PARAMS["align_resources_threads"])
    
    sample_id = os.path.basename(sentinel)[:-len(".sentinel")]

    sample = S.samples[sample_id]

    if sample.paired:
        fastq_input = "-1 " + ",".join(sample.fastq["read1"]) +\
                      " -2 " + ",".join(sample.fastq["read2"])

    else:
        fastq_input = "-U " + ",".join(sample.fastq["read1"])
    
    known_ss = ""
    if PARAMS["align_splice_sites"].lower() != "false":
    
        ss_file = os.path.join(os.path.dirname(PARAMS["align_index"]),
                               PARAMS["align_splice_sites"])
    
        known_ss = "--known-splicesite-infile=" + ss_file        
    
    novel_ss_outfile = os.path.join(t.outdir,
                                    sample_id + ".novel.splice.sites.txt")

    if sample.strand != "none":
        strand_param = "--rna-strandness %s" % sample.hisat_strand
    else:
        strand_param = ""

    statement = '''hisat2
                        -x %(align_index)s
                        %(fastq_input)s
                        --threads %(align_resources_threads)s
                        %(known_ss)s
                        --novel-splicesite-outfile %(novel_ss_outfile)s
                        %(strand_param)s
                        %(align_options)s
                        -S /dev/null
                        &> %(log_file)s;
                    gzip %(novel_ss_outfile)s
                ''' % dict(PARAMS, **t.var, **locals())

    P.run(statement)
    IOTools.touch_file(sentinel) 


@merge(firstPass, 
       "hisat.dir/annotations/novel.splice.sites.sentinel")
def novelSpliceSites(infiles, sentinel):
    '''
    Collect the novel splice sites into a single file.
'''
    t = T.setup(infiles[0], sentinel, PARAMS,
            memory="4G", cpu=1)

    junction_files = " ".join([x.replace(".sentinel",".novel.splice.sites.txt.gz") 
                               for x in infiles])

    out_path = sentinel.replace(".sentinel",".txt")

    # sort -k1,1 -T %(cluster_tmpdir)s
    statement = '''mkdir -p tmp.dir;
                   sort_dir=`mktemp -d -p tmp.dir`;
                   zcat %(junction_files)s
                   | sort -k1,1 -T $sort_dir
                   | uniq
                   > %(out_path)s;
                   rm -rf $sort_dir
                '''
                
    P.run(statement)
    IOTools.touch_file(sentinel) 


def hisat_second_pass_jobs():

    for sample_id in S.samples.keys():
    
        yield([None,
               os.path.join("hisat.dir",
                            sample_id + ".sentinel")])

@follows(novelSpliceSites)
@files(hisat_second_pass_jobs)
def secondPass(infile, sentinel):
    '''
    Align reads using HISAT with known and novel junctions.
    '''

    t = T.setup(infile, sentinel, PARAMS,
                memory=PARAMS["align_resources_memory"],
                cpu=PARAMS["align_resources_threads"])
    
    sample_id = os.path.basename(sentinel)[:-len(".sentinel")]

    sample = S.samples[sample_id]

    if sample.paired:
        fastq_input = "-1 " + ",".join(sample.fastq["read1"]) +\
                      " -2 " + ",".join(sample.fastq["read2"])

    else:
        fastq_input = "-U " + ",".join(sample.fastq["read1"])

    novel_splice_sites = "hisat.dir/annotation/novel.splice.sites.txt"

    if sample.strand != "none":
        strand_param = "--rna-strandness %s" % sample.hisat_strand
    else:
        strand_param = ""
        
    outfile = sentinel.replace(".sentinel",".bam")

    statement = '''mkdir -p tmp.dir;
                   sort_dir=`mktemp -d -p tmp.dir`;
                   hisat2
                      -x %(align_index)s
                      %(fastq_input)s
                      --threads %(align_resources_threads)s
                      --novel-splicesite-infile %(novel_splice_sites)s
                      %(strand_param)s
                      %(align_options)s
                   2> %(log_file)s
                   | samtools view - -bS
                   | samtools sort - -T $sort_dir -o %(outfile)s >>%(log_file)s;
                   samtools index %(outfile)s;
                   rm -rf $sort_dir;
                 ''' % dict(PARAMS, **t.var, **locals())

    P.run(statement)
    IOTools.touch_file(sentinel) 

@follows(secondPass, mkdir("api/hisat.dir"))
@files(glob.glob("hisat.dir/*.bam*"),"hisat.dir/api.sentinel")
def api(infiles, sentinel):

    for infile in infiles:
        os.symlink(os.path.join("..","..",infile), 
                   os.path.join("api", infile))
        
    IOTools.touch_file(sentinel)


@files(api, "hisat.dir/useBam.sentinel")
def useBam(infile, sentinel):
    '''
    Link the hisat2 BAM files to the api.dir/bam directory
    '''

    os.symlink("hisat.dir", "api/bam")
    
    IOTools.touch_file(sentinel)

# --------------------- < generic pipeline tasks > -------------------------- #

@follows(api)
def full():
    pass


def main(argv=None):
    if argv is None:
        argv = sys.argv
    P.main(argv)

if __name__ == "__main__":
    sys.exit(P.main(sys.argv))
