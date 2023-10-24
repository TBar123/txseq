"""===================
pipeline_ensembl.py
===================

Overview
--------

This pipeline post-processes Ensembl annotation files to prepare a set of annotation files suitable for the analysis of RNA-sequencing data. It performs the following tasks:

#. Makes a version of primary assembly FASTA file in which the Y chromosome PAR regions are hard masked.
#. Makes a coding and non-coding transcript FASTA file that only contains records for PRI contigs and that excludes transcript sequences from the Y chromosome PAR region (by filtering the Ensembl cdna and ncrna transcript fasta files)
#. Makes a filtered version of the Ensembl geneset GTF file that only contains records for PRI contigs and that excludes gene models from the Y chromosome PAR region.


Configuration
-------------

The pipeline requires a configured :file:`pipeline_ensembl.yml` file.

A default configuration file can be generated by executing: ::

   txseq ensembl config

Input files
-----------

The pipeline requires the following inputs

#. The Ensembl primary assembly FASTA sequences
#. The Ensembl geneset in GTF format
#. The Ensembl cDNA FASTA sequences
#. The Ensembl ncRNA FASTA sequences 
#. PAR region definitions in BED format

The location of these three files must be specified in the 'pipeline_ensembl.yml' file.

Output files
------------

The pipeline creates an "api" folder with the following files for use by downstream pipelines:

#. api/primary.assembly.fa.gz
#. api/transcripts.fa.gz
#. api/geneset.gtf.gz

Code
====

"""
from ruffus import *
import sys
import os
from pathlib import Path

# import CGAT-core pipeline functions
from cgatcore import pipeline as P
import cgatcore.iotools as IOTools


# Import txseq utility functions
import txseq.tasks as T

# ----------------------- < pipeline configuration > ------------------------ #

# Override function to collect config files
P.control.write_config_files = T.write_config_files

# load options from the yml file
P.parameters.HAVE_INITIALIZED = False
PARAMS = P.get_parameters(T.get_parameter_file(__file__))

# set the location of the code directory
PARAMS["txseq_code_dir"] = Path(__file__).parents[1]

# ---------------------- < specific pipeline tasks > ------------------------ #


# TODO: add basic sanity checks that input files exist...


@files(PARAMS["par"],
       "Y.PAR.bed")
def extractYPAR(infile, outfile):
    '''
    Make a BED file containing the coordinates of the PAR regions
    on the Y chromosome
    '''
    
    statement = '''grep Y %(infile)s > %(outfile)s
                ''' % locals()
                
    P.run(statement)
                


@transform(extractYPAR,
           regex(r"(.*).bed"),
           r"ypar.masked.primary.assembly.fa.sentinel")
def hardMaskYPAR(infile, sentinel):
    '''
       Hard mask the chromosome Y PAR region 
    '''
    
    y_par_bed = infile
    
    t = T.setup(y_par_bed, sentinel, PARAMS)

    
    statement = '''bedtools maskfasta
                   -fi <(zcat %(primary_assembly)s)
                   -fo %(out_file)s
                   -bed %(y_par_bed)s
                    &> %(log_file)s;
                    gzip %(out_file)s;
                ''' % dict(PARAMS, **t.var, **locals())
                
    P.run(statement, **t.resources)
    IOTools.touch_file(sentinel)    


@transform(hardMaskYPAR,
           regex(r"(.*).fa.sentinel"),
           r"contigs.sentinel")
def contigs(infile, sentinel):
    '''
    Get a list of the contigs present in the primary assembly
    '''
    
    assembly = infile.replace(".sentinel",".gz")
    
    t = T.setup(assembly, sentinel, PARAMS)
    
    statement = '''zgrep \> %(assembly)s
                   | sed 's/>//g'
                   > %(out_file)s
                ''' % dict(**t.var, **locals())

    P.run(statement, **t.resources)
    IOTools.touch_file(sentinel)  


@transform(contigs,
           regex(r".*.sentinel"),
           add_inputs(extractYPAR),
           "filtered.transcripts.fa.gz.sentinel")
def filteredTranscriptFasta(infiles, sentinel):
    '''
    Filter ensembl cdna & ncrna fasta files to exclude genes on non primary contigs
    and genes in the Y PAR region.
    '''
    
    contig_file_sentinel, ypar = infiles
    contig_file = contig_file_sentinel.replace(".sentinel","")

    t = T.setup(contig_file, sentinel, PARAMS)
        
    statement='''python %(txseq_code_dir)s/python/ensembl_filter_transcript_fasta.py
                 --ensembltxfasta=%(cdna)s,%(ncrna)s
                 --contigs=%(contig_file)s
                 --mask=%(ypar)s
                 --outfile=%(out_file)s
                 &> %(log_file)s
              ''' % dict(PARAMS, **t.var, **locals())
    
    P.run(statement, **t.resources)
    IOTools.touch_file(sentinel)


@transform(contigs,
           regex(r".*.sentinel"),
           add_inputs(extractYPAR),
           "filtered.geneset.gtf.gz.sentinel")
def filteredGTF(infiles, sentinel):
    '''
    Filter the ensembl geneset to exclude genes on non primary contigs
    and genes in the Y PAR region.
    '''
    
    contig_file_sentinel, ypar = infiles
    contig_file = contig_file_sentinel.replace(".sentinel","")

    t = T.setup(contig_file, sentinel, PARAMS)
        
    statement='''python %(txseq_code_dir)s/python/ensembl_filter_gtf.py
                 --ensemblgtf=%(geneset)s
                 --contigs=%(contig_file)s
                 --mask=%(ypar)s
                 --outfile=%(out_file)s
                 &> %(log_file)s
              ''' % dict(PARAMS, **t.var, **locals())
    
    P.run(statement, **t.resources)
    IOTools.touch_file(sentinel)


@transform(filteredGTF,
           regex(r".*.sentinel"),
           "transcript.to.gene.map.sentinel")
def transcriptToGeneMap(infile, sentinel):
    '''
    Make a map of transcripts to genes for use by salmon
    '''
    
    gtf = infile.replace(".sentinel","")

    t = T.setup(gtf, sentinel, PARAMS)
        
    statement='''zgrep transcript %(gtf)s
                 | sed 's/.*gene_id "\\([^"]*\\)".*transcript_id "\\([^"]*\\)".*$/\\2\\\t\\1/g' 
                 | sort -u > %(out_file)s
              ''' % dict(PARAMS, **t.var, **locals())
    
    P.run(statement, **t.resources)
    IOTools.touch_file(sentinel) 
    
@transform(filteredGTF,
           regex(r".*.sentinel"),
           "transcript.info.tsv.gz.sentinel")
def transcriptInfo(infile, sentinel):
    '''
    Extract transcript information from the GTF
    '''
    
    gtf = infile.replace(".sentinel","")

    t = T.setup(gtf, sentinel, PARAMS)
        
    statement='''python %(txseq_code_dir)s/python/ensembl_extract_gtf_attributes.py
                 --ensemblgtf=%(geneset)s
                 --attributes=transcript_id,transcript_name,transcript_biotype,gene_id,gene_name,gene_biotype
                 --outfile=%(out_file)s
                 &> %(log_file)s
              ''' % dict(PARAMS, **t.var, **locals())
    
    P.run(statement, **t.resources)
    IOTools.touch_file(sentinel) 


@follows(hardMaskYPAR,
         filteredTranscriptFasta,
         filteredGTF,
         transcriptToGeneMap,
         transcriptInfo)
@files(None,"api.sentinel")
def api(infile, sentinel):

    if not os.path.exists("api.dir"):
        os.mkdir("api.dir")

    pa = "ypar.masked.primary.assembly.fa.gz"
    
    if not os.path.exists(pa):
        raise ValueError("ypar masked primary assembly file not found")
    
    os.symlink(os.path.join("..",pa), 
               os.path.join("api.dir","txseq.genome.fa.gz"))
    
    txfa = "filtered.transcripts.fa.gz"
    
    if not os.path.exists(txfa):
        raise ValueError("filtered transcript fasta file not found")
        
    os.symlink(os.path.join("..",txfa), 
               os.path.join("api.dir","txseq.transcript.fa.gz"))
    
    gtf = "filtered.geneset.gtf.gz"
    
    if not os.path.exists(gtf):
        raise ValueError("filtered geneset gtf file not found")
        
    os.symlink(os.path.join("..", gtf), 
               os.path.join("api.dir","txseq.geneset.gtf.gz"))
    
    tx2gene = "transcript.to.gene.map"
    
    if not os.path.exists(tx2gene):
        raise ValueError("transcript-to-gene map file not found")
        
    os.symlink(os.path.join("..", tx2gene), 
               os.path.join("api.dir","txseq.transcript.to.gene.map"))
    
    txinfo = "transcript.info.tsv.gz"
    
    if not os.path.exists(txinfo):
        raise ValueError("transcript info file not found")
        
    os.symlink(os.path.join("..", txinfo), 
               os.path.join("api.dir","txseq.transcript.info.tsv.gz"))
    
    IOTools.touch_file(sentinel)


# --------------------- < generic pipeline tasks > -------------------------- #

@follows(api)
def full():
    '''Target to run the full pipeline'''
    pass


def main(argv=None):
    if argv is None:
        argv = sys.argv
    P.main(argv)

if __name__ == "__main__":
    sys.exit(P.main(sys.argv))