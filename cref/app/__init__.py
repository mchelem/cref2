import logging
import math
import os
import pickle
import time

import pandas
import matplotlib.pyplot as plt
# plt.style.use('ggplot')
from matplotlib.backends.backend_pdf import PdfPages

from Bio import pairwise2

from cref import sequence
from cref.sequence.alignment import Blast
from cref.structure.torsions import TorsionsCalculator
from cref.structure import write_pdb
from cref.structure.clustering import cluster_torsion_angles
from cref.structure.secondary import SecondaryStructurePredictor
from cref.structure.secondary import ss_eight_to_three
from cref.structure import plot

logger = logging.getLogger('CReF')

default_cref_params = dict(
    fragment_size=5,
    number_of_clusters=8,
    exclude=dict(
        identity=0,
        pdbs=[],
    ),
    max_templates=100,
)

default_blast_params = dict(
    expect_threshold=900000,
    number_of_alignments=500,
    word_size=2,
    scoring=dict(
        matrix='PAM30',
        gap_costs='ungapped',
    ),
)


class BaseApp:

    def __init__(self, params):
        self.set_params(params)
        self.blast = Blast(db='data/blastdb/pdbseqres')
        self.ss_predictor = SecondaryStructurePredictor('data/ss.db')
        self.torsions_calculator = TorsionsCalculator('data/torsions.db')
        self.failed_pdbs = []
        self.torsions = {}
        self.pdb_identities = {}

    def set_blast_params(self, params):
        logger.info('Prediction params: ' + str(params))
        blast_args = default_blast_params.copy()
        blast_args.update(params)

        self.blast_args = {
            'evalue': blast_args['expect_threshold'],
            'word_size': blast_args['word_size'],
            'matrix': blast_args['scoring']['matrix'],
            'num_alignments': blast_args['number_of_alignments']
        }

        gap_costs = blast_args['scoring']['gap_costs']
        if gap_costs == 'ungapped':
            self.blast_args['ungapped'] = True
        else:
            gap_open, gap_extend = [int(x) for x in gap_costs.split()]
            self.blast_args['gapopen'] = gap_open
            self.blast_args['gapextend'] = gap_extend

    def set_cref_params(self, params):
        self.params = default_cref_params.copy()
        self.params.update(params)

        self.fragment_size = self.params['fragment_size']
        self.central = math.floor(self.fragment_size / 2)
        self.number_of_clusters = self.params['number_of_clusters']
        self.max_templates = self.params['max_templates']
        if 'pdbs' in self.params['exclude']:
            self.excluded_pdbs = [x.lower()
                                  for x in self.params['exclude']['pdbs']]
        else:
            self.excluded_pdbs = []
        if 'identity' in self.params['exclude']:
            self.excluded_identity = self.params['exclude']['identity']
        else:
            self.excluded_identity = 0

    def set_params(self, params):
        self.set_blast_params(params.pop('blast', {}))
        self.set_cref_params(params)

    def reporter(self, state):
        logger.info(state[0] + state[1:].lower().replace('_', ' '))

    def get_central_angles(self, angles, hsp):
        central = self.central
        pos = central - hsp.query_start + 1
        subject_start = angles['residues'].find(hsp.sbjct)

        if pos >= 0 and subject_start >= 0:
            pos = subject_start + pos
            return (
                angles['residues'][pos],
                angles['phi'][pos],
                angles['psi'][pos]
            )
        return ('', None, None)

    def get_hsp_structure(self, fragment, ss, hsp, angles, index):
        identity = -1
        residue, phi, psi = self.get_central_angles(angles, hsp)

        pdb_dssp_result = self.ss_predictor.pdb_dssp(hsp.pdb_code, hsp.chain)
        if phi and psi and pdb_dssp_result:
            hsp_seq, hsp_ss = pdb_dssp_result
            start = hsp.sbjct_start - hsp.query_start
            end = hsp.sbjct_end + self.fragment_size - (
                hsp.query_end)
            frag_identity = round(
                100 * (hsp.identities / self.fragment_size))

            if self.excluded_identity > 0:
                if (hsp.pdb_code, hsp.chain) not in self.pdb_identities:
                    alignment = pairwise2.align.globalxx(
                        self.sequence,
                        hsp_seq,
                        one_alignment_only=True
                    )[0]
                    identity = (alignment[2] / alignment[4]) * 100
                    self.pdb_identities[(hsp.pdb_code, hsp.chain)] = identity
                else:
                    identity = self.pdb_identities[(hsp.pdb_code, hsp.chain)]

            if identity <= self.excluded_identity:
                if hsp_ss[start:end]:
                    return dict(
                        pdb=hsp.pdb_code,
                        chain=hsp.chain,
                        fragment=fragment,
                        fragment_ss=ss,
                        subject=hsp.sbjct,
                        subject_full=hsp_seq[start:end],
                        subject_ss=hsp_ss[start:end],
                        central_ss=hsp_ss[start:end][self.central],
                        identity=frag_identity,
                        score=hsp.score,
                        phi=round(phi, 2),
                        psi=round(psi, 2),
                    )
            else:
                logger.info('Skipping {}, identity {}  > {}'.format(
                    hsp.pdb_code.upper() + ':' + hsp.chain.upper(),
                    round(identity, 2),
                    self.excluded_identity)
                )
        return None

    def get_torsion_angles(self, pdb_code):
        if pdb_code not in self.torsions:
            angles = self.torsions_calculator.get_angles(pdb_code)
            self.torsions[pdb_code] = angles
        else:
            angles = self.torsions[pdb_code]
        return angles

    def get_structures_for_blast(self, fragment, ss, hsps, index):
        blast_structures = []
        hsps.sort(key=lambda hsp: (hsp.identities, hsp.score), reverse=True)
        for hsp in hsps:
            if len(blast_structures) < self.max_templates:
                try:
                    pdb_code = hsp.pdb_code
                    if pdb_code in self.excluded_pdbs:
                        logger.info('Skipping pdb {} (given in params)'.format(
                            pdb_code))
                    elif pdb_code not in self.failed_pdbs:
                        angles = self.get_torsion_angles(pdb_code)
                        structure = self.get_hsp_structure(
                            fragment, ss, hsp, angles, index)
                        if structure:
                            blast_structures.append(structure)
                except Exception as e:
                    self.failed_pdbs.append(pdb_code)
                    logger.debug(e)
            else:
                break

        return blast_structures

    def get_secondary_structure(self, aa_sequence, output_dir):
        self.reporter('PREDICTING_SECONDARY_STRUCTURE')
        prediction = self.ss_predictor.sspro(aa_sequence)
        ss = prediction.secondary_structure if prediction else None
        ss_fragments = list(sequence.fragment(ss, self.fragment_size))
        logger.info('Seq: ' + aa_sequence)
        logger.info('Str: ' + ss)
        with open(os.path.join(output_dir, 'secondary_structure.txt'), 'w') as \
                sequence_file:
            sequence_file.write(''.join([ss_eight_to_three(x) for x in ss]))
        ss_fragments = [x.replace('C', '-') for x in ss_fragments]
        return ss, ss_fragments

    def get_angles_for_fragment(self, fragment, ss, index, output_dir):
        logger.info('Fragment: ' + fragment)
        logger.info('Residue: ' + fragment[self.central])
        self.reporter('RUNNING_BLAST')
        hsps = self.blast.align(fragment, self.blast_args)

        self.reporter('RUNNING_TORSIONS')
        blast_structures = self.get_structures_for_blast(
            fragment, ss, hsps, index)
        blast_structures = pandas.DataFrame(
            blast_structures,
            columns=[
                'pdb', 'chain', 'fragment', 'subject', 'subject_full',
                'fragment_ss', 'subject_ss', 'central_ss', 'identity',
                'score', 'phi', 'psi'
            ]
        )
        self.reporter('CLUSTERING')
        if len(blast_structures) > self.max_templates:
            blast_structures = blast_structures[:self.max_templates]

        blast_structures.to_excel(
            self.excel_writer,
            sheet_name=fragment,
            index=False
        )
        target = self.params.get('pdb', None)
        plot.ramachandran(
            blast_structures,
            "{} ({})".format(fragment[self.central], fragment),
            target,
            output_writer=self.pdf_writer,
        )
        logger.info('Clustering {} fragments'.format(len(blast_structures)))
        return cluster_torsion_angles(
            blast_structures,
            ss[self.central],
            self.number_of_clusters,
            selector='ss',
            name="{} ({})".format(fragment[self.central], fragment),
            output_writer=self.pdf_writer,
            output_dir=output_dir,
        )

    def display_elapsed_time(self, start_time):
        elapsed_time = time.time() - start_time
        if elapsed_time > 60:
            logger.info('Prediction took {} minutes'.format(elapsed_time / 60))
        else:
            logger.info('Prediction took {} seconds'.format(elapsed_time))

    def log_dihedrals(self, angles, aa, ss, output_dir):
        plt.figure()
        logger.info('Dihedral angles')
        logger.info(('aa_seq\tss_seq\tphi_exp\tphi_prd'
                    '\tphi_dif\tpsi_exp\tpsi_prd\tpsi_dif'))

        experimental_torsions = self.get_torsion_angles(self.params['pdb'])
        exp_phi = experimental_torsions['phi']
        exp_psi = experimental_torsions['psi']
        # Avoid a big meaningless outlier
        exp_phi[0] = 180
        exp_psi[-1] = 180
        pred_phi = [float(x[0]) for x in angles]
        pred_psi = [float(x[1]) for x in angles]
        phi_diff = []
        psi_diff = []

        for i in range(len(angles)):
            try:
                phi_diff.append(180 - abs(180 - abs(exp_phi[i] - pred_phi[i])))
                psi_diff.append(180 - abs(180 - abs(exp_psi[i] - pred_psi[i])))
                logger.info('{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}'.format(
                    aa[i],
                    ss[i],
                    round(exp_phi[i], 2),
                    round(pred_phi[i], 2),
                    round(phi_diff[i], 2),
                    round(exp_psi[i], 2),
                    round(pred_psi[i], 2),
                    round(psi_diff[i], 2),
                ))
            except Exception as error:
                print(error)
                break
        plt.plot(range(len(phi_diff)), phi_diff, label='$\phi$', color='g')
        plt.plot(
            range(len(psi_diff)), psi_diff, label='$\psi$', linestyle='dashed')
        plt.xlabel("Resíduo")
        plt.ylabel("RMSD")
        plt.xticks(range(len(aa)), [x for x in aa])
        plt.legend()
        plt.savefig(
            os.path.join(output_dir, 'dihedrals.svg'), dpi=300, format='svg')
        plt.close()

    def log_inertias(self, inertias, aa, output_dir):
        plt.figure()
        plt.plot(range(len(aa)), inertias, label='$\phi$')
        plt.xlabel("Resíduo")
        plt.ylabel("Inércia")
        plt.xticks(range(len(aa)), [x for x in aa])
        plt.savefig(
            os.path.join(output_dir, 'inertias.svg'), dpi=300, format='svg')
        pickle.dump(
            inertias, open(os.path.join(output_dir, 'inertias.pkl'), 'wb'))

        plt.close()

    def run(self, aa_sequence, output_dir):
        self.sequence = aa_sequence
        report_dir = os.path.join(output_dir, 'report')

        if not os.path.exists(report_dir):
            os.makedirs(report_dir)
            os.makedirs(os.path.join(report_dir, 'ramachandran'))
            os.makedirs(os.path.join(report_dir, 'clustering'))

        self.excel_writer = pandas.ExcelWriter(
            os.path.join(report_dir, 'templates.xlsx'))

        self.reporter('STARTED')
        start_time = time.time()

        # Aminoacids in the beggining have unknown phi and psi
        dihedral_angles = [(180, 180)] * self.central
        inertias = [float('nan')] * self.central

        ss_seq, fragments_ss = self.get_secondary_structure(
            aa_sequence, output_dir)
        fragments = list(sequence.fragment(aa_sequence, self.fragment_size))
        fragments_len = len(fragments)
        logger.info("Number of fragments: {}".format(fragments_len))
        logger.info('-' * 30)
        pdf_report = os.path.join(report_dir, 'ramachandram_plots.pdf')
        with PdfPages(pdf_report) as self.pdf_writer:
            for i, fragment in enumerate(fragments):
                logger.info('Progress: {} of {} fragments'.format(
                    i + 1, fragments_len))
                ss = fragments_ss[i]

                angles, inertia = self.get_angles_for_fragment(
                    fragment, ss, i, report_dir)
                dihedral_angles.append(angles)
                inertias.append(inertia)
                logger.info('-' * 30)

        # Amino acids in the end have unbound angles
        dihedral_angles += [(180, 180)] * self.central
        inertias += [float('nan')] * self.central

        if 'pdb' in self.params:
            try:
                self.log_dihedrals(
                    dihedral_angles, aa_sequence, ss_seq, report_dir)
                self.log_inertias(inertias, aa_sequence, report_dir)
            except Exception as error:
                print(error)

        self.reporter('WRITING_PDB')
        output_file = os.path.join(output_dir, 'predicted_structure.pdb')
        write_pdb(aa_sequence, dihedral_angles, self.central, output_file)
        self.display_elapsed_time(start_time)
        self.excel_writer.close()
        return os.path.abspath(output_file)
