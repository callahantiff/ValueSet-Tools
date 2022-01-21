"""Main module
# Resources
- Reference google sheets:
  - Source data: https://docs.google.com/spreadsheets/d/1jzGrVELQz5L4B_-DqPflPIcpBaTfJOUTrVJT5nS_j18/edit#gid=1335629675
  - Source data (old): https://docs.google.com/spreadsheets/d/17hHiqc6GKWv9trcW-lRnv-MhZL8Swrx2/edit#gid=1335629675
  - Output example: https://docs.google.com/spreadsheets/d/1uroJbhMmOTJqRkTddlSNYleSKxw4i2216syGUSK7ZuU/edit?userstoinvite=joeflack4@gmail.com&actionButton=1#gid=435465078
"""
import json
import os
import pickle
from copy import copy
from datetime import datetime, timezone
from pathlib import Path
from random import randint
from typing import Dict, List, OrderedDict
from uuid import uuid4

import pandas as pd

from vsac_wrangler.config import CACHE_DIR, OUTPUT_DIR, PROJECT_ROOT
from vsac_wrangler.definitions.constants import FHIR_JSON_TEMPLATE
from vsac_wrangler.google_sheets import get_sheets_data
from vsac_wrangler.vsac_api import get_value_sets

# USER1: This is an actual ID to a valid user in palantir, who works on our BIDS team.
PALANTIR_ENCLAVE_USER_ID_1 = 'a39723f3-dc9c-48ce-90ff-06891c29114f'
VSAC_LABEL_PREFIX = '[VSAC Bulk-Import test] '


def _save_csv(df: pd.DataFrame, filename='output', subfolder=None, field_delimiter=',', ):
    """Side effects: Save CSV"""
    outdir = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(outdir):
        os.mkdir(outdir)
    outdir2 = os.path.join(outdir, datetime.now().strftime('%Y.%m.%d'))
    if not os.path.exists(outdir2):
        os.mkdir(outdir2)

    outdir3 = outdir2 if subfolder is None else os.path.join(outdir2, subfolder)
    if not os.path.exists(outdir3):
        os.mkdir(outdir3)

    output_format = 'csv' if field_delimiter == ',' else 'tsv' if field_delimiter == '\t' else 'txt'
    outpath = os.path.join(outdir3, f'{filename}.{output_format}')
    df.to_csv(outpath, sep=field_delimiter, index=False)


def _datetime_palantir_format() -> str:
    """Returns datetime str in format used by palantir data enclave
    e.g. 2021-03-03T13:24:48.000Z (milliseconds allowed, but not common in observed table)"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + 'Z'


# TODO: repurpose this to use VSAC format
# noinspection DuplicatedCode
def vsac_to_fhir(value_set: Dict) -> Dict:
    """Convert VSAC JSON dict to FHIR JSON dict"""
    # TODO: cop/paste FHIR_JSON_TEMPLATE literally here instead and use like other func
    d: Dict = copy(FHIR_JSON_TEMPLATE)
    d['id'] = int(value_set['valueSet.id'][0])
    d['text']['div'] = d['text']['div'].format(value_set['valueSet.description'][0])
    d['url'] = d['url'].format(str(value_set['valueSet.id'][0]))
    d['name'] = value_set['valueSet.name'][0]
    d['title'] = value_set['valueSet.name'][0]
    d['status'] = value_set['valueSet.status'][0]
    d['description'] = value_set['valueSet.description'][0]
    d['compose']['include'][0]['system'] = value_set['valueSet.codeSystem'][0]
    d['compose']['include'][0]['version'] = value_set['valueSet.codeSystemVersion'][0]
    concepts = []
    d['compose']['include'][0]['concept'] = concepts

    return d


# TODO:
def vsac_to_vsac(v: Dict, depth=2) -> Dict:
    """Convert VSAC JSON dict to OMOP JSON dict
    This is the format @DaveraGabriel specified by looking at the VSAC web interface."""
    # Attempt at regexp
    # Clinical Focus: Asthma conditions which suggest applicability of NHLBI NAEPP EPR3 Guidelines for the Diagnosis and
    # Management of Asthma (2007) and the 2020 Focused Updates to the Asthma Management Guidelines),(Data Element Scope:
    # FHIR Condition.code),(Inclusion Criteria: SNOMEDCT concepts in "Asthma SCT" and ICD10CM concepts in "Asthma
    # ICD10CM" valuesets.),(Exclusion Criteria: none)
    # import re
    # regexer = re.compile('\((.+): (.+)\)')  # fail
    # regexer = re.compile('\((.+): (.+)\)[,$]')
    # found = regexer.match(value_sets['ns0:Purpose'])
    # x1 = found.groups()[0]

    purposes = v['ns0:Purpose'].split('),')
    d = {
        "Concept Set Name": v['@displayName'],
        "Created At": 'vsacToOmopConversion:{}; vsacRevision:{}'.format(
            datetime.now().strftime('%Y/%m/%d'),
            v['ns0:RevisionDate']),
        "Created By": v['ns0:Source'],
        # "Created By": "https://github.com/HOT-Ecosystem/ValueSet-Converters",
        "Intention": {
            "Clinical Focus": purposes[0].split('(Clinical Focus: ')[1],
            "Inclusion Criteria": purposes[2].split('(Inclusion Criteria: ')[1],
            "Data Element Scope": purposes[1].split('(Data Element Scope: ')[1],
            "Exclusion Criteria": purposes[3].split('(Exclusion Criteria: ')[1],
        },
        "Limitations": {
            "Exclusion Criteria": "",
            "VSAC Note": None,  # VSAC Note: (exclude if null)
        },
        "Provenance": {
            "Steward": "",
            "OID": "",
            "Code System(s)": [],
            "Definition Type": "",
            "Definition Version": "",
        }
    }
    # TODO: use depth to make this either nested JSON, or, if depth=1, concatenate
    #  ... all intention sub-fields into a single string, etc.
    if depth == 1:
        d['Intention'] = ''
    elif depth < 1 or depth > 2:
        raise RuntimeError(f'vsac_to_vsac: depth parameter valid range: 1-2, but depth of {depth} was requested.')

    return d


def get_vsac_csv(
    value_sets: List[OrderedDict], google_sheet_name=None, field_delimiter=',', code_delimiter='|', filename='vsac_csv'
) -> pd.DataFrame:
    """Convert VSAC hiearchical XML in a VSAC-oriented tabular file"""
    rows = []
    for value_set in value_sets:
        code_system_codes = {}
        name = value_set['@displayName']
        purposes = value_set['ns0:Purpose'].split('),')
        purposes2 = []
        for p in purposes:
            i1 = 1 if p.startswith('(') else 0
            i2 = -1 if p[len(p) - 1] == ')' else len(p)
            purposes2.append(p[i1:i2])
        for concept_dict in value_set['ns0:ConceptList']['ns0:Concept']:
            code = concept_dict['@code']
            code_system = concept_dict['@codeSystemName']
            if code_system not in code_system_codes:
                code_system_codes[code_system] = []
            code_system_codes[code_system].append(code)

        for code_system, codes in code_system_codes.items():
            row = {
                'name': name,
                'nameVSAC': '[VSAC] ' + name,
                'oid': value_set['@ID'],
                'codeSystem': code_system,
                'limitations': purposes2[3],
                'intention': '; '.join(purposes2[0:3]),
                'provenance': '; '.join([
                    'Steward: ' + value_set['ns0:Source'],
                    'OID: ' + value_set['@ID'],
                    'Code System(s): ' + ','.join(list(code_system_codes.keys())),
                    'Definition Type: ' + value_set['ns0:Type'],
                    'Definition Version: ' + value_set['@version'],
                    'Accessed: ' + str(datetime.now())[0:-7]
                ]),
            }
            if len(codes) < 2000:
                row['codes'] = code_delimiter.join(codes)
            else:
                row['codes'] = code_delimiter.join(codes[0:1999])
                if len(codes) < 4000:
                    row['codes2'] = code_delimiter.join(codes[2000:])
                else:
                    row['codes2'] = code_delimiter.join(codes[2000:3999])
                    row['codes3'] = code_delimiter.join(codes[4000:])

            rows.append(row)

    # Create/Return DF & Save CSV
    df = pd.DataFrame(rows)
    _save_csv(df, filename=filename, subfolder=google_sheet_name, field_delimiter=field_delimiter)

    return df

def extract_from_vsac_for_palantir(value_sets: List[OrderedDict]) -> Dict[str, pd.DataFrame]:
    for value_set in value_sets:
        # will let palantir verify ID is indeed unique:
        oid__codeset_id_map[value_set['@ID']] = randint(0, 1000000000)
        for concept_dict in value_set['ns0:ConceptList']['ns0:Concept']:
            code = concept_dict['@code']
            code_system = concept_dict['@codeSystemName']
            if code_system not in codesystem_code__concept_id_map:
                codesystem_code__concept_id_map[code_system] = {}
            # will let palantir verify ID is indeed unique:
            codesystem_code__concept_id_map[code_system][code] = randint(0, 1000000000)

    # 1. Palantir enclave table: concept_set_version_item_rv_edited
    #       this stuff ends up in the concept_set_members table
    rows1 = []
    for value_set in value_sets:
        for concept_dict in value_set['ns0:ConceptList']['ns0:Concept']:
            code = concept_dict['@code']
            code_system = concept_dict['@codeSystemName']
            # The 3 fields isExcluded, includeDescendants, and includeMapped, are from OMOP but also in VSAC. If it has
            # ...these 3 options, it is intensional. And when you execute these 3, it is now extensional / expansion.
            row = {
                'codeset_id': oid__codeset_id_map[value_set['@ID']],
                'concept_id': '',  # leave blank for now
                # <non-palantir fields>
                'code': code,
                'codeSystem': code_system,
                # </non-palantir fields>
                'isExcluded': False,
                'includeDescendants': True,
                'includeMapped': False,
                'item_id': str(uuid4()),  # will let palantir verify ID is indeed unique
                'annotation': 'Generated from VSAC export',
                # 'created_by': 'DI&H Bulk Import',
                'created_by': PALANTIR_ENCLAVE_USER_ID_1,
                'created_at': _datetime_palantir_format()
            }
            rows1.append(row)
    df1 = pd.DataFrame(rows1)
    all[filename1] = df1
    _save_csv(df1, filename=filename1, subfolder=google_sheet_name, field_delimiter=field_delimiter)


def save_for_palantir_3csv(all: Dict[str, pd.DataFrame]):
    pass

def get_palantir_csv(
    value_sets: List[OrderedDict], google_sheet_name=None, field_delimiter=',',
    filename1='concept_set_version_item_rv_edited', filename2='code_sets', filename3='concept_set_container_edited'
) -> Dict[str, pd.DataFrame]:
    """Convert VSAC hiearchical XML to CSV compliant w/ Palantir's OMOP-inspired concept set editor data model"""
    # I. Create IDs that will be shared between files
    codesystem_code__concept_id_map = {}  # currently unused; as no common field in these 3 tables
    oid__codeset_id_map = {}
    for value_set in value_sets:
        # will let palantir verify ID is indeed unique:

        # TODO: work out with @Amin how we should actually be dealing with codeset set ids --
        #   we should at least make them larger that the largest existing.
        oid__codeset_id_map[value_set['@ID']] = randint(0, 1000000000)

        for concept_dict in value_set['ns0:ConceptList']['ns0:Concept']:
            code = concept_dict['@code']
            code_system = concept_dict['@codeSystemName']
            if code_system not in codesystem_code__concept_id_map:
                codesystem_code__concept_id_map[code_system] = {}
            # will let palantir verify ID is indeed unique:
            codesystem_code__concept_id_map[code_system][code] = randint(0, 1000000000)
            # @Joe: What is this random number supposed to be? I don't think we have
            #   reason to be using random numbers in any of this. It's totally confusing --
            #   someone might try to figure out what it actually references, and it doesn't
            #   reference anything

    # II. Create & save exports
    all = {}
    # 1. Palantir enclave table: concept_set_version_item_rv_edited
    rows1 = []
    for value_set in value_sets:
        for concept_dict in value_set['ns0:ConceptList']['ns0:Concept']:
            code = concept_dict['@code']
            code_system = concept_dict['@codeSystemName']
            # The 3 fields isExcluded, includeDescendants, and includeMapped, are from OMOP but also in VSAC. If it has
            # ...these 3 options, it is intensional. And when you execute these 3, it is now extensional / expansion.
            row = {
                'codeset_id': oid__codeset_id_map[value_set['@ID']],
                'concept_id': '',  # leave blank for now
                # <non-palantir fields>
                'code': code,
                'codeSystem': code_system,
                # </non-palantir fields>
                'isExcluded': False,
                'includeDescendants': True,
                'includeMapped': False,
                'item_id': str(uuid4()),  # will let palantir verify ID is indeed unique
                'annotation': 'Generated from VSAC export',
                # 'created_by': 'DI&H Bulk Import',
                'created_by': PALANTIR_ENCLAVE_USER_ID_1,
                'created_at': _datetime_palantir_format()
            }
            rows1.append(row)
    df1 = pd.DataFrame(rows1)
    all[filename1] = df1
    _save_csv(df1, filename=filename1, subfolder=google_sheet_name, field_delimiter=field_delimiter)

    # TODO: Find: Acute severe exacerbation of asthma co-occurrent with allergic rhinitis (disorder)
    # TODO: Why so few rows; correct here

    # 2. Palantir enclave table: code_sets
    rows2 = []
    for value_set in value_sets:
        concept_set_name = VSAC_LABEL_PREFIX + value_set['@displayName']
        purposes = value_set['ns0:Purpose'].split('),')
        purposes2 = []
        for p in purposes:
            i1 = 1 if p.startswith('(') else 0
            i2 = -1 if p[len(p) - 1] == ')' else len(p)
            purposes2.append(p[i1:i2])
        code_system_codes = {}
        for concept_dict in value_set['ns0:ConceptList']['ns0:Concept']:
            code = concept_dict['@code']
            code_system = concept_dict['@codeSystemName']
            if code_system not in code_system_codes:
                code_system_codes[code_system] = []
            code_system_codes[code_system].append(code)
        row = {
            'codeset_id': oid__codeset_id_map[value_set['@ID']],
            'concept_set_name': concept_set_name,
            'concept_set_version_title': concept_set_name + ' (v1)',
            'project': 'RP-4A9E',  # always use this project id for bulk import
            'source_application': 'EXTERNAL VSAC',
            'source_application_version': '',  # nullable
            'created_at': _datetime_palantir_format(),
            'atlas_json': '',  # nullable
            'is_most_recent_version': True,
            'version': 1,
            'comments': 'Exported from VSAC and bulk imported to N3C.',
            'intention': '; '.join(purposes2[0:3]),  # nullable
            'limitations': purposes2[3],  # nullable
            'issues': '',  # nullable
            'update_message': 'Initial version.',  # nullable (maybe?)
            # status field stats as appears in the code_set table 2022/01/12:
            # 'status': [
            #     '',  # null
            #     'Finished',
            #     'In Progress',
            #     'Awaiting Review',
            #     'In progress',
            # ][2],
            # status field doesn't show this in stats in code_set table, but UI uses this value by default:
            'status': 'Under Construction',
            'has_review': '',  # boolean (nullable)
            'reviewed_by': '',  # nullable
            'created_by': PALANTIR_ENCLAVE_USER_ID_1,
            'provenance': '; '.join([
                    'Steward: ' + value_set['ns0:Source'],
                    'OID: ' + value_set['@ID'],
                    'Code System(s): ' + ','.join(list(code_system_codes.keys())),
                    'Definition Type: ' + value_set['ns0:Type'],
                    'Definition Version: ' + value_set['@version'],
                    'Accessed: ' + str(datetime.now())[0:-7]
                ]),
            'atlas_json_resource_url': '',  # nullable
            # null, initial version will not have the parent version so this field would be always null:
            'parent_version_id': '',  # nullable
            # True ( after the import view it from the concept set editor to review the concept set and click done.
            # We can add the comments like we imported from VSAC and reviewed it from the concept set editor. )
            # 1. import 2. manual check 3 click done to finish the definition. - if we want to manually review them
            # first and click Done:
            'is_draft': True,
        }
        rows2.append(row)
    df2 = pd.DataFrame(rows2)
    all[filename2] = df2
    _save_csv(df2, filename=filename2, subfolder=google_sheet_name, field_delimiter=field_delimiter)

    # 3. Palantir enclave table: concept_set_container_edited
    rows3 = []
    for value_set in value_sets:
        purposes = value_set['ns0:Purpose'].split('),')
        purposes2 = []
        for p in purposes:
            i1 = 1 if p.startswith('(') else 0
            i2 = -1 if p[len(p) - 1] == ')' else len(p)
            purposes2.append(p[i1:i2])
        concept_set_name = VSAC_LABEL_PREFIX + value_set['@displayName']
        for concept_dict in value_set['ns0:ConceptList']['ns0:Concept']:
            # I'm surprised these aren't used in the enclave `concept_set_container_edited` table:
            # code = concept_dict['@code']
            # code_system = concept_dict['@codeSystemName']

            row = {
                'concept_set_id': concept_set_name,
                'concept_set_name': concept_set_name,
                'project_id': '',  # nullable
                'assigned_informatician': PALANTIR_ENCLAVE_USER_ID_1,  # nullable
                'assigned_sme': PALANTIR_ENCLAVE_USER_ID_1,  # nullable
                'status': ['Finished', 'Under Construction', 'N3C Validation Complete'][1],
                'stage': [
                    'Finished',
                    'Awaiting Editing',
                    'Candidate for N3C Review',
                    'Awaiting N3C Committee Review',
                    'Awaiting SME Review',
                    'Under N3C Committee Review',
                    'Under SME Review',
                    'N3C Validation Complete',
                    'Awaiting Informatician Review',
                    'Under Informatician Review',
                ][1],
                'intention': '; '.join(purposes2[0:3]),
                'n3c_reviewer': '',  # nullable
                'alias': concept_dict['@displayName'],
                'archived': False,
                # 'created_by': 'DI&H Bulk Import',
                'created_by': PALANTIR_ENCLAVE_USER_ID_1,
                'created_at': _datetime_palantir_format()
            }
            rows3.append(row)
    df3 = pd.DataFrame(rows3)
    all[filename3] = df3
    _save_csv(df3, filename=filename3, subfolder=google_sheet_name, field_delimiter=field_delimiter)

    return all


def run(
    input_source_type=['google-sheet', 'oids-txt'][1],
    google_sheet_name=None,
    output_format=['tabular/csv', 'json'][0],
    output_structure=['fhir', 'vsac'][1],
    tabular_field_delimiter=[',', '\t'][0],
    tabular_intra_field_delimiter=[',', ';', '|'][2],
    json_indent=4, use_cache=False
):
    """Main function
    Refer to interfaces/cli.py for argument descriptions."""
    value_sets = []
    pickle_file = Path(CACHE_DIR, f'value_sets - from {input_source_type}.pickle')

    if use_cache:
        if pickle_file.is_file() and use_cache:
            value_sets = pickle.load(open(pickle_file, 'rb'))
        else:
            use_cache = False
    if not use_cache:
        # 1. Get OIDs to query
        # TODO: Get a different API_Key for this than my 'ohbehave' project
        if input_source_type == 'google-sheet':
            df: pd.DataFrame = get_sheets_data(google_sheet_name)
            object_ids: List[str] = [x for x in list(df['OID']) if x != '']
        elif input_source_type == 'oids-txt':
            with open(f'{PROJECT_ROOT}/input/oids.txt', 'r') as f:
                object_ids: List[str] = [oid.rstrip() for oid in f.readlines()]

        value_sets: List[OrderedDict] = get_value_sets(object_ids)

        with open(pickle_file, 'wb') as handle:
            pickle.dump(value_sets, handle, protocol=pickle.HIGHEST_PROTOCOL)

    if output_format == 'tabular/csv':
        if output_structure == 'vsac':
            get_vsac_csv(value_sets, google_sheet_name, tabular_field_delimiter, tabular_intra_field_delimiter)
        elif output_structure == 'palantir-concept-set-tables':
            get_palantir_csv(value_sets, google_sheet_name, tabular_field_delimiter)
        elif output_structure == 'fhir':
            raise NotImplementedError('output_structure "fhir" not available for output_format "csv/tabular".')
    elif output_format == 'json':
        # Populate JSON objs
        d_list: List[Dict] = []
        for value_set in value_sets:
            value_set2 = {}
            if output_structure == 'fhir':
                value_set2 = vsac_to_fhir(value_set)
            elif output_structure == 'vsac':
                value_set2 = vsac_to_vsac(value_set)
            elif output_structure == 'atlas':  # TODO: Implement
                raise NotImplementedError('For "atlas" output-structure, output-format "json" not yet implemented.')
            d_list.append(value_set2)

        # Save file
        for d in d_list:
            if 'name' in d:
                valueset_name = d['name']
            else:
                valueset_name = d['Concept Set Name']
            valueset_name = valueset_name.replace('/', '|')
            filename = valueset_name + '.json'
            filepath = os.path.join(OUTPUT_DIR, filename)
            with open(filepath, 'w') as fp:
                if json_indent:
                    json.dump(d, fp, indent=json_indent)
                else:
                    json.dump(d, fp)
