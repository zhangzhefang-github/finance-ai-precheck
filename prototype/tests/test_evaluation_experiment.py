from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from prototype.evaluation_experiment import (
    EvaluationStore, capture_sources, compare_results, digest, publish, read_json, read_record,
)
from prototype.expense_precheck import AmountPolicy

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / 'prototype/examples/evaluation-v1'


def example(group, name):
    return read_json(EXAMPLES / group / (name + '.json'))


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = EvaluationStore(Path(self.temp.name) / 'evaluation')
        for path in sorted((EXAMPLES / 'cases').glob('*.json')):
            self.store.import_case(read_json(path))
        for name in ('REV-MISSING', 'REV-FIXED', 'REV-AMOUNT', 'REV-NEGATION', 'REV-MODEL'):
            self.store.import_revision(example('revisions', name))

    def run_case(self, name='strict', case='CASE-AMOUNT', revision='REV-AMOUNT', **kwargs):
        return self.store.run(case, revision, example('executions', name), **kwargs)

    def report(self, run):
        self.assertEqual('COMPLETED', run['execution_status'], run['outcome'])
        return run['outcome']['report_snapshot']

    def test_actual_strategy_and_parameter_changes_preserve_old_results(self):
        a = self.run_case(run_id='a')
        original = (self.store.run_dir('a') / 'outcome.json').read_bytes()
        b = self.run_case('tolerance-005', run_id='b', parent_run_id='a', run_reason='RULE_CHANGE')
        c = self.run_case('tolerance-002', run_id='c', parent_run_id='b', run_reason='RULE_CHANGE')
        self.assertEqual(['REVIEW', 'PASS', 'REVIEW'], [self.report(x)['final_status'] for x in (a,b,c)])
        self.assertEqual(a['start']['input_sha256'], b['start']['input_sha256'])
        self.assertNotEqual(self.report(b)['input_fingerprint'], self.report(c)['input_fingerprint'])
        self.assertIn('MOCK', self.report(b)['disclaimer'])
        ab = self.store.compare('a', 'b')
        self.assertEqual(['RULE'], ab['changed_dimensions'])
        self.assertEqual('CONTROLLED_RULE_CHANGE', ab['comparability'])
        self.assertEqual(['PRECHECK-AMOUNT-001'], [x['check_id'] for x in ab['differences']['checks']])
        self.assertEqual([], ab['differences']['input_changes'])
        self.assertEqual('CONTROLLED_RULE_CHANGE', self.store.compare('b', 'c')['comparability'])
        self.assertEqual(original, (self.store.run_dir('a') / 'outcome.json').read_bytes())

    def test_missing_input_revision_and_mixed_changes(self):
        a = self.run_case(case='CASE-CORRECTION', revision='REV-MISSING', run_id='missing')
        b = self.run_case(case='CASE-CORRECTION', revision='REV-FIXED', run_id='fixed', parent_run_id='missing', run_reason='INPUT_REVISION')
        self.assertEqual('MISSING_EVIDENCE', self.report(a)['final_status'])
        self.assertEqual('PASS', self.report(b)['final_status'])
        comp = self.store.compare('missing', 'fixed')
        self.assertEqual('CONTROLLED_INPUT_CHANGE', comp['comparability'])
        self.assertEqual(['/claim/claim_amount'], [x['path'] for x in comp['differences']['input_changes']])
        self.run_case('tolerance-005', case='CASE-CORRECTION', revision='REV-FIXED', run_id='mixed', parent_run_id='missing', run_reason='INPUT_REVISION')
        mixed = self.store.compare('missing', 'mixed')
        self.assertEqual(['INPUT', 'RULE'], mixed['changed_dimensions'])
        self.assertEqual('MIXED_CHANGE', mixed['comparability'])
        self.assertTrue(any('intent' in text for text in mixed['limitations']))

    def evaluation(self, target, expected=None):
        return {'evaluator_id':'tester','target':target, 'verdict':'CORRECT','scope_relation':'IN_SCOPE',
                'expected':expected, 'reason':'offline contract verification',
                'basis':{'kind':'IMPLEMENTATION_CONTRACT','ref':'evaluation-v1','quote':'preserve observable execution'},
                'evidence_refs':[{'root':'input','pointer':'/claim_id'}]}

    def test_all_states_accept_evaluation_and_no_report_failure_has_no_decision(self):
        configs = [('review', 'strict','CASE-AMOUNT','REV-AMOUNT'),
                   ('pass','strict','CASE-CORRECTION','REV-FIXED'),
                   ('missing','strict','CASE-CORRECTION','REV-MISSING'),
                   ('failed','provider-failure','CASE-CORRECTION','REV-FIXED')]
        for rid, config, case, revision in configs:
            with self.subTest(rid=rid):
                run = self.run_case(config,case,revision,run_id=rid)
                target = {'kind':'EXECUTION'} if rid=='failed' else {'kind':'DECISION'}
                evaluation = self.store.evaluate(rid, self.evaluation(target))
                self.assertEqual(rid,evaluation['run_id'])
                self.assertEqual(1,len(self.store.show(rid)['human_evaluations']))
                if rid=='failed':
                    self.assertEqual('FAILED',run['execution_status'])
                    self.assertIsNone(run['outcome']['precheck_run_id'])
                    self.assertIsNone(run['outcome']['report_snapshot'])
                    self.assertEqual('PRECHECK',run['outcome']['failure']['stage'])
                    self.assertEqual('MOCK_PROVIDER_FAILURE',run['outcome']['failure']['code'])
                    with self.assertRaises(ValueError):
                        self.store.evaluate(rid, self.evaluation({'kind':'DECISION'}))
                    with self.assertRaises(ValueError):
                        self.store.evaluate(rid, self.evaluation({'kind':'FIELD','root':'report','pointer':'/final_status'}))

    def test_real_baseline_pass_missed_issue_evidence_and_disagreement(self):
        run = self.run_case(case='CASE-NEGATION',revision='REV-NEGATION',run_id='negation')
        self.assertEqual('PASS',self.report(run)['final_status'])
        original = (self.store.run_dir('negation')/'outcome.json').read_bytes()
        evaluation = example('evaluations','pass-missed')
        first = self.store.evaluate('negation',evaluation)
        self.assertEqual('PASS',first['observed']['result'])
        self.assertEqual('MISSED_ISSUE',first['finding_kind'])
        disputed = deepcopy(evaluation)
        disputed.update(evaluator_id='another-reviewer', verdict='UNDETERMINED', scope_relation='UNDETERMINED', expected=None)
        self.store.evaluate('negation',disputed)
        self.assertTrue(self.store.show('negation')['disputed_targets'])
        corrected = deepcopy(disputed)
        corrected.update(evaluator_id='local-reviewer',supersedes_evaluation_id=first['evaluation_id'])
        self.store.evaluate('negation',corrected)
        self.assertFalse(self.store.show('negation')['disputed_targets'])
        self.assertEqual(3,len(self.store.evaluations('negation')))
        self.assertEqual(original,(self.store.run_dir('negation')/'outcome.json').read_bytes())
        bad = deepcopy(evaluation);bad['observed']={'result':'REVIEW'}
        with self.assertRaises(ValueError): self.store.evaluate('negation',bad)
        bad = deepcopy(evaluation);bad['evidence_refs']=[{'root':'input','pointer':'/absent'}]
        with self.assertRaises(ValueError): self.store.evaluate('negation',bad)
        bad = deepcopy(evaluation);bad['target']['check_id']='nonexistent'
        with self.assertRaises(ValueError): self.store.evaluate('negation',bad)
        bad = deepcopy(evaluation);bad['basis']['quote']='invented scope statement'
        with self.assertRaises(ValueError): self.store.evaluate('negation',bad)

    def test_uncovered_and_undetermined_are_not_in_scope_errors(self):
        self.run_case(run_id='a')
        value=self.evaluation({'kind':'DECISION'})
        value.update(verdict='NOT_COVERED',scope_relation='OUT_OF_SCOPE',reason='Invoice authenticity is outside the declared scope')
        self.store.evaluate('a',value)
        value.update(verdict='INCORRECT',expected={'final_status':'REVIEW'})
        with self.assertRaises(ValueError): self.store.evaluate('a',value)
        value.update(verdict='UNDETERMINED',scope_relation='UNDETERMINED')
        self.store.evaluate('a',value)
        value.update(verdict='INCORRECT',scope_relation='IN_SCOPE')
        value['basis']['kind']='UNCONFIRMED_OPINION'
        with self.assertRaises(ValueError): self.store.evaluate('a',value)

    def test_model_timeout_is_completed_not_no_report_failure(self):
        run=self.run_case('model-timeout','CASE-MODEL','REV-MODEL',run_id='timeout')
        report=self.report(run)
        self.assertEqual('REVIEW',report['final_status'])
        self.assertEqual('MODEL_REQUEST_FAILED',report['technical_reasons'][0]['code'])
        self.assertTrue(report['model']['invoked'])
        self.assertIsNone(run['outcome']['failure'])
        self.store.evaluate('timeout',self.evaluation({'kind':'CHECK','check_id':'PRECHECK-SEMANTIC-001'}))

    def test_invalid_strategy_is_recorded_before_service_execution(self):
        cfg=example('executions','strict');cfg['amount_policy']['implementation_id']='invented-v2'
        with patch('prototype.evaluation_experiment.ExpensePrecheckService') as service:
            run=self.store.run('CASE-AMOUNT','REV-AMOUNT',cfg,run_id='bad-config')
        service.assert_not_called()
        self.assertEqual('FAILED',run['execution_status'])
        self.assertEqual('RESOLVE_CONFIG',run['outcome']['failure']['stage'])
        self.assertIsNone(run['resolved'])
        self.assertFalse((self.store.run_dir('bad-config')/'execution.sqlite3').exists())

    def test_input_checks_evidence_and_extraction_values_are_fingerprinted(self):
        revision=example('revisions','REV-AMOUNT')
        for key in ('fields','field_sources','checks'):
            with self.subTest(key=key):
                changed=deepcopy(revision)
                changed['revision_id']='CHANGED-'+key
                changed['parent_revision_id']='REV-AMOUNT'
                if key=='fields': changed['payload']['invoice_extraction']['fields']['total_amount']='900.00'
                elif key=='field_sources': changed['payload']['invoice_extraction']['field_sources']['total_amount'][0]['raw_text']='changed evidence'
                else: changed['payload']['invoice_checks'][0]['result']='REVIEW'
                with self.assertRaisesRegex(ValueError,'fingerprint'):
                    self.store.import_revision(changed)
        changed.pop('input_sha256')
        stored=self.store.import_revision(changed)
        self.assertNotEqual(revision['input_sha256'],stored['input_sha256'])
        branch=deepcopy(changed);branch['revision_id']='illegal-branch'
        with self.assertRaises(ValueError):self.store.import_revision(branch)

    def test_append_only_record_and_run_ids(self):
        self.run_case(run_id='a')
        original=(self.store.run_dir('a')/'outcome.json').read_bytes()
        with self.assertRaises(FileExistsError):self.run_case(run_id='a')
        with self.assertRaises(FileExistsError):self.store.import_case(example('cases','CASE-AMOUNT'))
        evaluation=self.evaluation({'kind':'DECISION'});evaluation['evaluation_id']='E1'
        self.store.evaluate('a',evaluation)
        with self.assertRaises(FileExistsError):self.store.evaluate('a',evaluation)
        self.assertEqual(original,(self.store.run_dir('a')/'outcome.json').read_bytes())
        with self.assertRaises(ValueError):self.store.load_run('../a')

    def test_repeat_ignores_ids_and_timestamps_and_cross_case_is_rejected(self):
        a=self.run_case(run_id='a')
        b=self.run_case(run_id='b',parent_run_id='a',run_reason='REPEAT')
        self.assertNotEqual(self.report(a)['precheck_run_id'],self.report(b)['precheck_run_id'])
        comp=self.store.compare('a','b')
        self.assertEqual('SAME_CONDITIONS_REPEAT',comp['comparability'])
        self.assertFalse(comp['differences']['results_changed'])
        self.run_case(case='CASE-NEGATION',revision='REV-NEGATION',run_id='c')
        with self.assertRaises(ValueError):self.store.compare('a','c')
        with self.assertRaises(ValueError):self.run_case(case='CASE-NEGATION',revision='REV-NEGATION',parent_run_id='a')

    def test_network_and_business_database_remain_unused(self):
        sentinel=Path(self.temp.name)/'business.sqlite3';sentinel.write_bytes(b'not touched')
        with patch.dict(os.environ,{'DOCUMENT_DB_PATH':str(sentinel),'AGICTO_API_KEY':'do-not-use'}), patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')):
            run=self.run_case(run_id='isolated')
        self.assertEqual('COMPLETED',run['execution_status'])
        self.assertEqual(b'not touched',sentinel.read_bytes())
        self.assertTrue((self.store.run_dir('isolated')/'execution.sqlite3').exists())
        self.assertIsNone(run['resolved']['prompt']['id'])

    def test_unfinished_recovery_does_not_rerun_and_is_not_controlled(self):
        real_capture=__import__('prototype.evaluation_experiment',fromlist=['capture_report']).capture_report
        with patch('prototype.evaluation_experiment.capture_report',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.run_case(run_id='interrupted')
        self.assertEqual('UNFINISHED',self.store.load_run('interrupted')['execution_status'])
        with patch('prototype.evaluation_experiment.ExpensePrecheckService') as service:
            recovered=self.store.recover('interrupted')
        service.assert_not_called()
        self.assertEqual('COMPLETED',recovered['execution_status'])
        self.assertEqual('RECOVERED',recovered['outcome']['capture_method'])
        self.assertIsNone(recovered['outcome']['ended_at'])
        self.assertEqual('UNKNOWN',recovered['outcome']['implementation_integrity'])
        self.assertIsNotNone(real_capture(self.store.run_dir('interrupted'),recovered['input'],recovered['resolved']))
        with self.assertRaises(ValueError):self.store.recover('interrupted')
        self.run_case(run_id='new')
        self.assertEqual('INCOMPLETE',self.store.compare('interrupted','new')['comparability'])

    def test_interruption_before_report_and_start_publication_failure(self):
        with patch('prototype.evaluation_experiment.load_facts',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.run_case(run_id='no-report')
        recovered=self.store.recover('no-report')
        self.assertEqual('FAILED',recovered['execution_status'])
        self.assertEqual('RUN_INTERRUPTED',recovered['outcome']['failure']['code'])
        with patch('prototype.evaluation_experiment.publish',side_effect=OSError('read-only')), patch('prototype.evaluation_experiment.ExpensePrecheckService') as service:
            with self.assertRaises(OSError):self.run_case(run_id='no-start')
        service.assert_not_called()

    def test_capture_error_preserves_known_report(self):
        from prototype.evaluation_experiment import capture_report
        calls=0
        def flaky(*args):
            nonlocal calls
            calls+=1
            if calls==1:raise OSError('temporary capture failure')
            return capture_report(*args)
        with patch('prototype.evaluation_experiment.capture_report',side_effect=flaky):
            run=self.run_case(run_id='capture-error')
        self.assertEqual('FAILED',run['execution_status'])
        self.assertIsNotNone(run['outcome']['report_snapshot'])
        self.assertEqual('CAPTURE_RESULT',run['outcome']['failure']['stage'])
        self.run_case(run_id='normal')
        self.assertEqual('INCOMPLETE',self.store.compare('capture-error','normal')['comparability'])

    def test_tampered_artifact_is_not_compared_as_controlled(self):
        self.run_case(run_id='a');self.run_case(run_id='b')
        path=self.store.run_dir('b')/'input.json'
        value=read_json(path);value['payload']['claim']['claim_amount']='999.99'
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):self.store.load_run('b')
        comp=self.store.compare('a','b')
        self.assertEqual('INCOMPLETE',comp['comparability'])
        self.assertEqual({},comp['differences'])

    def test_source_change_during_run_and_no_git_head(self):
        before=capture_sources();after=deepcopy(before);after['source_sha256']='changed'
        with patch('prototype.evaluation_experiment.capture_sources',side_effect=[before,after]), patch('prototype.evaluation_experiment.git_identity',return_value={'git_commit':None,'git_state':'NO_HEAD'}):
            run=self.run_case(run_id='changed')
        self.assertIsNone(run['start']['git_commit'])
        self.assertEqual('CHANGED_DURING_RUN',run['outcome']['implementation_integrity'])
        self.assertTrue(read_record(self.store.run_dir('changed')/'source-manifest.json')['files'])
        self.run_case(run_id='normal')
        self.assertEqual('INCOMPLETE',self.store.compare('changed','normal')['comparability'])

    def test_unused_model_configuration_is_still_a_changed_condition(self):
        self.run_case(run_id='a')
        self.run_case('model-timeout',run_id='b')  # baseline determines transport; fake never called
        comp=self.store.compare('a','b')
        self.assertEqual(['MODEL'],comp['changed_dimensions'])
        self.assertEqual('MIXED_CHANGE',comp['comparability'])
        self.assertTrue(any('neither Run invoked' in x for x in comp['limitations']))

    def test_same_version_labels_do_not_hide_implementation_change(self):
        first=self.run_case(run_id='a')
        historical=capture_sources()
        historical['files']['prototype/expense_precheck.py']='synthetic-other-source-digest'
        historical['source_sha256']=digest(historical['files'])
        with patch('prototype.evaluation_experiment.capture_sources',return_value=historical), patch('prototype.evaluation_experiment.LOADED_SOURCE_SHA256',historical['source_sha256']):
            second=self.run_case(run_id='b')
        self.assertEqual(first['resolved']['labels'],second['resolved']['labels'])
        comp=self.store.compare('a','b')
        self.assertEqual(['IMPLEMENTATION'],comp['changed_dimensions'])
        self.assertEqual('MIXED_CHANGE',comp['comparability'])

    def test_reuses_existing_golden_bad_failure_catalog(self):
        catalog=read_json(ROOT/'prototype/examples/precheck-regression-cases.json')
        for index,item in enumerate(catalog['cases']):
            case=example('cases','CASE-AMOUNT');case['case_id']=f'REGRESSION-{index}'
            self.store.import_case(case)
            revision=example('revisions','REV-FIXED')
            revision.update(case_id=case['case_id'],revision_id=f'REGRESSION-REV-{index}',parent_revision_id=None)
            revision.pop('input_sha256')
            claim=deepcopy(item['claim']);claim.update(invoice_extraction_id='eval-extraction',data_classification='MOCK')
            revision['payload'].update(claim_id=claim['claim_id'],claim=claim)
            self.store.import_revision(revision)
            report=self.report(self.run_case(case=case['case_id'],revision=revision['revision_id']))
            self.assertEqual(item['expected_status'],report['final_status'])
            self.assertEqual(item['expected_route'],report['semantic_judgment']['route'])

    def test_missing_check_is_not_fabricated_and_no_report_has_no_check_diff(self):
        a=self.run_case(run_id='a')
        b=deepcopy(a)
        b['outcome']['report_snapshot']['deterministic_checks']=[c for c in self.report(b)['deterministic_checks'] if c['check_id']!='PRECHECK-CURRENCY-001']
        diff=compare_results(a,b)
        self.assertEqual(1,len(diff['checks']))
        self.assertIsNone(diff['checks'][0]['after'])
        self.assertFalse(diff['checks'][0]['after_present'])
        self.run_case('provider-failure',run_id='failed')
        comp=self.store.compare('a','failed')
        self.assertFalse(comp['differences']['report_comparison_available'])
        self.assertEqual([],comp['differences']['checks'])

    def test_recovery_rejects_multiple_or_mismatched_reports(self):
        with patch('prototype.evaluation_experiment.capture_report',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.run_case(run_id='ambiguous')
        from prototype.document_ingestion import Repository
        repository=Repository(self.store.run_dir('ambiguous')/'execution.sqlite3',migrate=False)
        with repository.connect() as db:
            row=dict(db.execute('SELECT * FROM precheck_runs').fetchone())
            row['id']='unexpected-second-report'
            db.execute(f"INSERT INTO precheck_runs ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",list(row.values()))
        with self.assertRaisesRegex(ValueError,'unique report'):self.store.recover('ambiguous')
        self.assertEqual('UNFINISHED',self.store.load_run('ambiguous')['execution_status'])

    def test_invalid_json_is_rejected_before_execution(self):
        for body in ('{"x":1,"x":2}', '{"x":NaN}', '[]'):
            path=Path(self.temp.name)/'invalid.json';path.write_text(body)
            with self.assertRaises(ValueError):read_json(path)
        revision=example('revisions','REV-AMOUNT')
        revision.update(revision_id='FLOAT-AMOUNT',parent_revision_id='REV-AMOUNT')
        revision['payload']['claim']['claim_amount']=100.00
        revision.pop('input_sha256')
        with self.assertRaises(ValueError):self.store.import_revision(revision)

    def test_permanent_capture_failure_keeps_returned_report_id(self):
        with patch('prototype.evaluation_experiment.capture_report',side_effect=OSError('capture unavailable')):
            run=self.run_case(run_id='capture-unavailable')
        self.assertEqual('FAILED',run['execution_status'])
        self.assertIsNone(run['outcome']['report_snapshot'])
        self.assertIsNotNone(run['outcome']['precheck_run_id'])
        self.assertIn('capture_error',run['outcome']['failure'])

    def test_cli_failure_exit_status_and_show_no_mutation(self):
        command=[sys.executable,'-m','prototype.evaluation_experiment','--workspace',str(self.store.root)]
        result=subprocess.run(command+['run','--case','CASE-AMOUNT','--revision','REV-AMOUNT','--config',str(EXAMPLES/'executions/provider-failure.json'),'--id','cli-fail'],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(2,result.returncode,result.stderr)
        self.assertEqual('FAILED',json.loads(result.stdout)['execution_status'])
        paths=list(self.store.root.rglob('*'))
        original={str(p):p.read_bytes() for p in paths if p.is_file()}
        result=subprocess.run(command+['show','cli-fail'],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(2,result.returncode)
        self.assertEqual(original,{str(p):p.read_bytes() for p in paths if p.is_file()})

    def test_cli_compare_summary_saves_full_comparison(self):
        self.run_case(run_id='strict')
        self.run_case('tolerance-005',run_id='tolerant')
        result=subprocess.run([sys.executable,'-m','prototype.evaluation_experiment','--workspace',str(self.store.root),
            'compare','strict','tolerant','--id','summary','--summary'],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertIn('CONTROLLED_RULE_CHANGE',result.stdout)
        self.assertIn('REVIEW -> PASS',result.stdout)
        self.assertTrue(read_record(self.store.path('comparisons','summary'))['differences']['checks'])


class AmountPolicyTests(unittest.TestCase):
    def check(self, amount, total, tolerance='0.05', currency='CNY', evidence=True):
        claim={'claim_amount':amount,'currency':currency,'data_classification':'MOCK'}
        invoice={'total_amount':total,'field_sources_json':json.dumps({'total_amount':[{'raw_text':'￥'+total}]} if evidence else {})}
        return AmountPolicy('mock.amount.absolute_tolerance.v1',tolerance).check(claim,invoice)

    def test_inclusive_boundaries_and_sign(self):
        for left,right,tol,expected in [('100.00','100.05','0.05','PASS'),('100.05','100.00','0.05','PASS'),('100.00','100.06','0.05','REVIEW'),('100.00','100.00','0.00','PASS'),('100.01','100.00','0.00','REVIEW')]:
            with self.subTest(left=left,right=right,tol=tol):
                result=self.check(left,right,tol)
                self.assertEqual(expected,result['result'])
                self.assertTrue(result['values']['amount_policy']['evaluated'])

    def test_invalid_parameters_missing_values_and_currency(self):
        for bad in ('NaN','Infinity','-0.01','0.001',0.05,True,None):
            with self.subTest(bad=bad),self.assertRaises(ValueError):AmountPolicy('mock.amount.absolute_tolerance.v1',bad)
        self.assertEqual('MISSING_EVIDENCE',self.check(None,'100.00')['result'])
        self.assertEqual('MISSING_EVIDENCE',self.check('100.00','100.00',evidence=False)['result'])
        result=self.check('100.00','100.00',currency='USD')
        self.assertEqual('REVIEW',result['result'])
        self.assertFalse(result['values']['amount_policy']['evaluated'])
        with self.assertRaises(ValueError):AmountPolicy('mock.amount.absolute_tolerance.v1','0.05').check({'claim_amount':'100.00'},None)
        with self.assertRaises(ValueError):AmountPolicy('unknown.v2')
        with self.assertRaises(ValueError):AmountPolicy(tolerance='0.00')


if __name__=='__main__':
    unittest.main()
