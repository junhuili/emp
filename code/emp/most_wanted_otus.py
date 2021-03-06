#!/usr/bin/env python
from __future__ import division

__author__ = "Jai Ram Rideout"
__copyright__ = "Copyright 2012, The QIIME project"
__credits__ = ["Jai Ram Rideout", "Greg Caporaso", "Meg Pirrung"]
__license__ = "GPL"
__version__ = "1.5.0-dev"
__maintainer__ = "Jai Ram Rideout"
__email__ = "jai.rideout@gmail.com"
__status__ = "Development"

"""Contains functions used in the most_wanted_otus.py script."""

from collections import defaultdict
from itertools import cycle
from operator import itemgetter
from os import makedirs
from os.path import basename, join, normpath, splitext
from pickle import dump
from tempfile import NamedTemporaryFile

from pylab import axes, figlegend, figure, legend, pie, savefig

from biom.parse import parse_biom_table

from cogent import DNA, LoadSeqs
from cogent.app.blast import blast_seqs, Blastall
from cogent.app.formatdb import build_blast_db_from_fasta_path
from cogent.parse.blast import BlastResult
from cogent.parse.fasta import MinimalFastaParser
from cogent.util.misc import remove_files

from qiime.colors import data_colors, data_color_order
from qiime.parse import parse_mapping_file_to_dict
from qiime.util import (add_filename_suffix, parse_command_line_parameters,
        get_options_lookup, make_option, qiime_system_call)
from qiime.workflow.util import generate_log_fp, WorkflowError, WorkflowLogger

html_header = '<html lang="en"><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8"> <title>Most Wanted OTUs</title><link rel="stylesheet" type="text/css" href="most_wanted_otus.css"></head><body>'
html_footer = '</body></html>'

def generate_most_wanted_list(output_dir, otu_table_fps, rep_set_fp, gg_fp,
        nt_fp, mapping_fp, mapping_category, top_n, min_abundance,
        max_abundance, min_categories, num_categories_to_plot,
        max_gg_similarity, max_nt_similarity, e_value, word_size,
        merged_otu_table_fp, suppress_taxonomic_output, jobs_to_start,
        command_handler, status_update_callback, force):
    try:
        makedirs(output_dir)
    except OSError:
        if not force:
            raise WorkflowError("Output directory '%s' already exists. Please "
                    "choose a different directory, or force overwrite with -f."
                    % output_dir)

    logger = WorkflowLogger(generate_log_fp(output_dir))
    commands, blast_results_fp, rep_set_cands_failures_fp, \
        master_otu_table_ms_fp = _get_most_wanted_filtering_commands(
            output_dir, otu_table_fps,
            rep_set_fp, gg_fp, nt_fp, mapping_fp, mapping_category,
            min_abundance, max_abundance, min_categories, max_gg_similarity,
            e_value, word_size, merged_otu_table_fp, jobs_to_start)

    # Execute the commands, but keep the logger open because
    # we're going to write additional status updates as we process the data.
    command_handler(commands, status_update_callback, logger,
                    close_logger_on_success=False)
    commands = []

    # We'll sort the BLAST results by percent identity (ascending) and pick the
    # top n.
    logger.write("Reading in BLAST results, sorting by percent identity, "
                 "and picking the top %d OTUs.\n\n" % top_n)
    top_n_mw = _get_top_n_blast_results(open(blast_results_fp, 'U'), top_n,
                                        max_nt_similarity)

    # Read in our filtered down candidate seqs file and latest filtered and
    # collapsed OTU table. We'll need to compute some stats on these to include
    # in our report.
    logger.write("Reading in filtered candidate sequences and latest filtered "
                 "and collapsed OTU table.\n\n")
    mw_seqs = _get_rep_set_lookup(open(rep_set_cands_failures_fp, 'U'))
    master_otu_table_ms = parse_biom_table(open(master_otu_table_ms_fp, 'U'))

    # Write results out to tsv and HTML table.
    logger.write("Writing most wanted OTUs results to TSV and HTML "
                 "tables.\n\n")
    output_img_dir = join(output_dir, 'img')
    try:
        makedirs(output_img_dir)
    except OSError:
        # It already exists, which is okay since we already know we are in
        # 'force' mode from above.
        pass

    tsv_lines, html_table_lines, mw_fasta_lines, plot_fps, plot_data_fps = \
            _format_top_n_results_table(top_n_mw,
                mw_seqs, master_otu_table_ms, output_img_dir, mapping_category,
                suppress_taxonomic_output, num_categories_to_plot)

    mw_tsv_rel_fp = 'most_wanted_otus.txt'
    mw_tsv_fp = join(output_dir, mw_tsv_rel_fp)
    mw_tsv_f = open(mw_tsv_fp, 'w')
    mw_tsv_f.write(tsv_lines)
    mw_tsv_f.close()

    mw_fasta_rel_fp = 'most_wanted_otus.fasta'
    mw_fasta_fp = join(output_dir, mw_fasta_rel_fp)
    mw_fasta_f = open(mw_fasta_fp, 'w')
    mw_fasta_f.write(mw_fasta_lines)
    mw_fasta_f.close()

    html_dl_links = ('<a href="%s" target="_blank">Download table in tab-'
            'separated value (TSV) format</a><br /><a href="%s" '
            'target="_blank">Download OTU sequence data in FASTA format</a>' %
            (mw_tsv_rel_fp, mw_fasta_rel_fp))
    html_lines = '%s<div>%s<br /><br />%s<br />%s</div>%s' % (html_header, html_dl_links,
                  html_table_lines, html_dl_links, html_footer)
    
    mw_html_f = open(join(output_dir, 'most_wanted_otus.html'), 'w')
    mw_html_f.write(html_lines)
    mw_html_f.close()
    logger.close()

def _get_most_wanted_filtering_commands(output_dir, otu_table_fps, rep_set_fp,
        gg_fp, nt_fp, mapping_fp, mapping_category, min_abundance,
        max_abundance, min_categories, max_gg_similarity, e_value, word_size,
        merged_otu_table_fp, jobs_to_start):
    commands = []
    otu_tables_to_merge = []

    if merged_otu_table_fp is None:
        for otu_table_fp in otu_table_fps:
            # First filter to keep only new (non-GG) OTUs.
            novel_otu_table_fp = join(output_dir, add_filename_suffix(otu_table_fp,
                                                                      '_novel'))
            commands.append([('Filtering out all GG reference OTUs',
                    'filter_otus_from_otu_table.py -i %s -o %s -e %s' %
                    (otu_table_fp, novel_otu_table_fp, gg_fp))])

            # Next filter to keep only abundant otus in the specified range
            # (looking only at extremely abundant OTUs has the problem of yielding
            # too many that are similar to stuff in the nt database).
            novel_abund_otu_table_fp = join(output_dir,
                    add_filename_suffix(novel_otu_table_fp, '_min%d_max%d' %
                    (min_abundance, max_abundance)))
            commands.append([('Filtering out all OTUs that do not fall within the '
                    'specified abundance threshold',
                    'filter_otus_from_otu_table.py -i %s -o %s -n %d -x %d' %
                    (novel_otu_table_fp, novel_abund_otu_table_fp, min_abundance,
                     max_abundance))])

            # Remove samples from the table that aren't in the mapping file.
            novel_abund_filtered_otu_table_fp = join(output_dir,
                    add_filename_suffix(novel_abund_otu_table_fp,
                    '_known_samples'))
            commands.append([('Filtering out samples that are not in the mapping '
                    'file',
                    'filter_samples_from_otu_table.py -i %s -o %s '
                    '--sample_id_fp %s' % (novel_abund_otu_table_fp,
                        novel_abund_filtered_otu_table_fp, mapping_fp))])

            # Next, collapse by mapping_category.
            otu_table_by_samp_type_fp = join(output_dir,
                    add_filename_suffix(novel_abund_filtered_otu_table_fp, '_%s' %
                    mapping_category))
            commands.append([('Collapsing OTU table by %s' % mapping_category,
                    'summarize_otu_by_cat.py -c %s -o %s -m %s -i %s' %
                    (novel_abund_filtered_otu_table_fp, otu_table_by_samp_type_fp,
                     mapping_category, mapping_fp))])
            otu_tables_to_merge.append(otu_table_by_samp_type_fp)

        # Merge all collapsed OTU tables.
        master_otu_table_fp = join(output_dir,
                'master_otu_table_novel_min%d_max%d_%s.biom' %
                (min_abundance, max_abundance, mapping_category))
        commands.append([('Merging collapsed OTU tables',
                'merge_otu_tables.py -i %s -o %s' %
                (','.join(otu_tables_to_merge), master_otu_table_fp))])
    else:
        master_otu_table_fp = merged_otu_table_fp

    # Filter to contain only otus in the specified minimum number of sample
    # types.
    master_otu_table_ms_fp = join(output_dir, add_filename_suffix(
            master_otu_table_fp, '_ms%d' % min_categories))
    commands.append([('Filtering OTU table to include only OTUs that appear '
            'in at least %d sample groups' % min_categories,
            'filter_otus_from_otu_table.py -i %s -o %s -s %d' %
            (master_otu_table_fp, master_otu_table_ms_fp, min_categories))])

    # Now that we have a filtered down OTU table of good candidate OTUs, filter
    # the corresponding representative set to include only these candidate
    # sequences.
    rep_set_cands_fp = join(output_dir,
            add_filename_suffix(rep_set_fp, '_candidates'))
    commands.append([('Filtering representative set to include only the '
            'latest candidate OTUs',
            'filter_fasta.py -f %s -o %s -b %s' %
            (rep_set_fp, rep_set_cands_fp, master_otu_table_ms_fp))])

    # Find the otus that don't hit GG at a certain maximum similarity
    # threshold.
    uclust_output_dir = join(output_dir, 'most_wanted_candidates_%s_%s' %
            (basename(gg_fp), str(max_gg_similarity)))
    commands.append([('Running uclust to get list of sequences that don\'t '
            'hit the maximum GG similarity threshold',
            'parallel_pick_otus_uclust_ref.py -i %s -o %s -r %s -s %s -O %d' %
            (rep_set_cands_fp, uclust_output_dir, gg_fp,
             str(max_gg_similarity), jobs_to_start))])

    # Filter the rep set to only include the failures from uclust.
    rep_set_cands_failures_fp = join(output_dir,
            add_filename_suffix(rep_set_cands_fp, '_failures'))
    commands.append([('Filtering candidate sequences to only include uclust '
            'failures',
            'filter_fasta.py -f %s -s %s -o %s' %
            (rep_set_cands_fp, join(uclust_output_dir,
             splitext(basename(rep_set_cands_fp))[0] + '_failures.txt'),
             rep_set_cands_failures_fp))])

    # BLAST the failures against nt.
    blast_output_dir = join(output_dir, 'blast_output')
    commands.append([('BLASTing filtered candidate sequences against nt '
            'database',
            'parallel_blast.py -i %s -o %s -r %s -D -e %f -w %d -O %d' %
            (rep_set_cands_failures_fp, blast_output_dir, nt_fp, e_value,
             word_size, jobs_to_start))])

    blast_results_fp = join(blast_output_dir,
            splitext(basename(rep_set_cands_failures_fp))[0] +
                              '_blast_out.txt')

    return commands, blast_results_fp, rep_set_cands_failures_fp, \
           master_otu_table_ms_fp

def _get_top_n_blast_results(blast_results_f, top_n, max_nt_similarity):
    """blast_results should only contain a single hit per query sequence"""
    result = []
    seen_otus = {}
    for line in blast_results_f:
        # Skip headers and comments.
        line = line.strip()
        if line and not line.startswith('#'):
            otu_id, subject_id, percent_identity = line.split('\t')[:3]
            percent_identity = float(percent_identity)

            # Skip otus that are too similar to their subject, and skip
            # duplicate query hits.
            if ((percent_identity / 100.0) <= max_nt_similarity and
                otu_id not in seen_otus):
                result.append((otu_id, subject_id, percent_identity))
                seen_otus[otu_id] = True
    return sorted(result, key=itemgetter(2))[:top_n]

def _get_rep_set_lookup(rep_set_f):
    result = {}
    for seq_id, seq in MinimalFastaParser(rep_set_f):
        seq_id = seq_id.strip().split()[0]
        result[seq_id] = seq
    return result

def _format_top_n_results_table(top_n_mw, mw_seqs, master_otu_table_ms,
                                output_img_dir, mapping_category,
                                suppress_taxonomic_output,
                                num_categories_to_plot):
    tsv_lines = ''
    html_lines = ''
    mw_fasta_lines = ''
    plot_fps = []
    plot_data_fps = []

    tsv_lines += '#\tOTU ID\tSequence\t'
    if not suppress_taxonomic_output:
        tsv_lines += 'Greengenes taxonomy\t'
    tsv_lines += 'NCBI nt closest match\tNCBI nt % identity\n'

    html_lines += ('<table id="most_wanted_otus_table" border="border">'
                   '<tr><th>#</th><th>OTU</th>')
    if not suppress_taxonomic_output:
        html_lines += '<th>Greengenes taxonomy</th>'
    html_lines += ('<th>NCBI nt closest match</th>'
                   '<th>Abundance by %s</th></tr>' % mapping_category)

    for mw_num, (otu_id, subject_id, percent_identity) in enumerate(top_n_mw):
        # Grab all necessary information to be included in our report.
        seq = mw_seqs[otu_id]

        mw_fasta_lines += '>%s\n%s\n' % (otu_id, seq)

        # Splitting code taken from
        # http://code.activestate.com/recipes/496784-split-string-into-n-
        #   size-pieces/
        split_seq = [seq[i:i+40] for i in range(0, len(seq), 40)]

        if not suppress_taxonomic_output:
            tax = master_otu_table_ms.ObservationMetadata[
                master_otu_table_ms.getObservationIndex(otu_id)]['taxonomy']

        gb_id = subject_id.split('|')[3]
        ncbi_link = 'http://www.ncbi.nlm.nih.gov/nuccore/%s' % gb_id

        # Compute the abundance of each most wanted OTU in each sample
        # grouping and create a pie chart to go in the HTML table.
        samp_types = master_otu_table_ms.SampleIds
        counts = master_otu_table_ms.observationData(otu_id)
        plot_data = _format_pie_chart_data(samp_types, counts,
                                           num_categories_to_plot)

        # Piechart code based on:
        # http://matplotlib.sourceforge.net/examples/pylab_examples/
        #   pie_demo.html
        # http://www.saltycrane.com/blog/2006/12/example-pie-charts-using-
        #   python-and/
        figure(figsize=(8,8))
        ax = axes([0.1, 0.1, 0.8, 0.8])
        patches = pie(plot_data[0], colors=plot_data[2], shadow=True)

        # We need a relative path to the image.
        pie_chart_filename = 'abundance_by_%s_%s.png' % (mapping_category,
                                                         otu_id)
        pie_chart_rel_fp = join(basename(normpath(output_img_dir)),
                pie_chart_filename)
        pie_chart_abs_fp = join(output_img_dir, pie_chart_filename)
        savefig(pie_chart_abs_fp, transparent=True)
        plot_fps.append(pie_chart_abs_fp)

        # Write out pickled data for easy plot editing post-creation.
        plot_data_fp = join(output_img_dir, 'abundance_by_%s_%s.p' %
                (mapping_category, otu_id))
        dump(plot_data, open(plot_data_fp, 'wb'))
        plot_data_fps.append(plot_data_fp)

        tsv_lines += '%d\t%s\t%s\t' % (mw_num + 1, otu_id, seq)
        if not suppress_taxonomic_output:
            tsv_lines += '%s\t' % tax
        tsv_lines += '%s\t%s\n' % (gb_id, percent_identity)

        html_lines += '<tr><td>%d</td><td><pre>&gt;%s\n%s</pre></td>' % (
                mw_num + 1, otu_id, '\n'.join(split_seq))
        if not suppress_taxonomic_output:
            html_lines += '<td>%s</td>' % tax
        html_lines += ('<td><a href="%s" target="_blank">%s</a> '
                '(%s%% sim.)</td>' % (ncbi_link, gb_id, percent_identity))

        # Create the legend as a table- couldn't get mpl to correctly
        # plot legend side-by-side the pie chart and don't have time to mess
        # with it anymore.
        legend_html = _format_legend_html(plot_data)
        html_lines += ('<td><table><tr><td><img src="%s" width="300" '
                'height="300" /></td><td>%s</td></tr></table></tr>' % (
                pie_chart_rel_fp, legend_html))
    html_lines += '</table>'

    return tsv_lines, html_lines, mw_fasta_lines, plot_fps, plot_data_fps

def _format_pie_chart_data(labels, data, max_count):
    if len(labels) != len(data):
        raise ValueError("The number of labels does not match the number "
                         "of counts.")
    colors = cycle([data_colors[color].toHex() for color in data_color_order])
    result = [(val, label, colors.next()) for val, label in zip(data, labels)]
    result = sorted(result, key=itemgetter(0), reverse=True)[:max_count]
    total = sum([e[0] for e in result])
    result = [(val / total, label, color) for val, label, color in result]
    return ([e[0] for e in result],
            ['%s (%.2f%%)' % (e[1], e[0] * 100.0) for e in result],
            [e[2] for e in result])

def _format_legend_html(plot_data):
    result = '<ul class="most_wanted_otus_legend">'
    for val, label, color in zip(plot_data[0], plot_data[1], plot_data[2]):
        result += ('<li><div class="key" style="background-color:%s"></div>%s</li>' % (color,label))
    return result + '</ul>'

# def _format_legend_html(plot_data):
#     result = '<table class="most_wanted_otus_legend">'
#     for val, label, color in zip(plot_data[0], plot_data[1], plot_data[2]):
#         result += ('<tr><td bgcolor="%s" width="50">&nbsp;</td>'
#                    '<td>%s</td></tr>' % (color, label))
#     return result + '</table>'
