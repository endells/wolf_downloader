"""FF adapter tests with Flask and a minimal scheduler/settings fixture.
Run: python -m pip install Flask; python -m unittest discover -s tests -v
"""
import importlib
import json
import logging
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

try:
    from flask import Flask, request, session
    from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


@unittest.skipUnless(HAS_FLASK, 'Flask is required for adapter/UI tests')
class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = {}
        self.scheduler = Mock()
        self.scheduler.is_include.return_value = False
        self.ff = types.ModuleType('framework')
        self.ff.F = types.SimpleNamespace(config={'path_data': self.tmp.name}, scheduler=self.scheduler)
        self.ff.Job = lambda *args: types.SimpleNamespace(args=args)
        plugin = types.ModuleType('plugin')
        class Base:
            def __init__(obj,P,**kw):
                obj.P=P;obj.name=kw['name'];obj.desc=kw['scheduler_desc']
            def get_scheduler_name(obj):return 'wolf_downloader_basic'
            def get_scheduler_desc(obj):return obj.desc
        plugin.PluginModuleBase = Base
        self.modules = patch.dict(sys.modules, {'framework':self.ff, 'plugin':plugin})
        self.modules.start()
        sys.modules.pop('wolf_downloader.mod_basic',None)
        mod = importlib.import_module('wolf_downloader.mod_basic')
        from wolf_downloader.common import DEFAULTS
        self.settings.update(DEFAULTS)
        self.settings['download_path']=self.tmp.name+'/downloads'
        P=types.SimpleNamespace(ModelSetting=types.SimpleNamespace(get=lambda key:self.settings.get(key), set=lambda key,value:self.settings.__setitem__(key,value)), package_name='wolf_downloader',logger=logging.getLogger('test'))
        self.module=mod.ModuleBasic(P)
        self.module.manager=Mock()
        self.module.manager.start.return_value='job-1'
        self.module.manager.status.return_value={'status':'idle','active':False}
        self.app=Flask(__name__)
        self.app.secret_key='test-only'
        self.app.jinja_loader=ChoiceLoader([FileSystemLoader(str(ROOT/'templates')),DictLoader({'base.html':'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body>{% block content %}{% endblock %}</body></html>'})])
        self.client=self.app.test_client()
        @self.app.route('/wolf_downloader/basic/<page>')
        def menu(page):return self.module.process_menu(page,request)
        @self.app.route('/wolf_downloader/ajax/basic/command',methods=['POST'])
        def command():return self.module.process_command(request.form['command'], request.form.get('arg1'), request.form.get('arg2'), '', request)
        with self.client.session_transaction() as s:s['wolf_downloader_csrf']='token'

    def tearDown(self):
        self.modules.stop();self.tmp.cleanup()

    def call(self,command,arg1='',arg2=''):
        return self.client.post('/wolf_downloader/ajax/basic/command',data={'command':command,'arg1':arg1,'arg2':arg2,'csrf_token':'token'}).get_json()

    def test_all_pages_render_and_javascript_parses(self):
        for page in ('setting','manual','browse','status','history','log'):
            with self.subTest(page=page):
                response=self.client.get('/wolf_downloader/basic/'+page)
                self.assertEqual(response.status_code,200)
                html=response.get_data(as_text=True)
                self.assertIn('작업 상태',html)
                js=html.split('<script>',1)[1].split('</script>',1)[0]
                script=Path(self.tmp.name)/'page.js';script.write_text(js)
                result=subprocess.run(['node','--check',str(script)],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)

    def test_settings_sections_and_duplicate_action_locations(self):
        html = self.client.get('/wolf_downloader/basic/setting').get_data(as_text=True)
        from html.parser import HTMLParser
        class Panels(HTMLParser):
            def __init__(self):
                super().__init__(); self.tabs=[]; self.panels=[]
            def handle_starttag(self, tag, attrs):
                attrs=dict(attrs)
                if attrs.get('role')=='tab':self.tabs.append(attrs)
                if attrs.get('role')=='tabpanel':self.panels.append(attrs)
        parsed=Panels();parsed.feed(html)
        self.assertEqual([x['data-settings-tab'] for x in parsed.tabs], ['basic','advanced','files'])
        self.assertEqual([('hidden' in x) for x in parsed.panels], [False,True,True])
        self.assertNotIn('<details>',html)
        self.assertNotIn('<summary>',html)
        self.assertLess(html.index('id="tab-basic"'),html.index('id="save_top"'))
        self.assertLess(html.index('id="save_top"'),html.index('설정 저장은 다운로드'))
        self.assertGreater(html.index('id="save"'),html.index('id="panel-files"'))
        self.assertIn("bind('save_top',saveSettings)",html)
        self.assertIn("bind('save',saveSettings)",html)
        for page in ('manual','browse','status','history'):
            other=self.client.get('/wolf_downloader/basic/'+page).get_data(as_text=True)
            self.assertNotIn('role="tablist"',other)

    def test_save_and_auto_add_do_not_start_download(self):
        config=self.module.config()
        config['titles']='42'
        self.assertEqual(self.call('save',json.dumps(config))['ret'],'success')
        self.assertEqual(self.call('add_auto','43')['ret'],'success')
        self.assertEqual(self.settings['titles'],'42\n43')
        self.module.manager.start.assert_not_called()

    def test_enabling_schedule_does_not_run_immediately(self):
        config=self.module.config();config['basic_auto_start']=True
        self.assertEqual(self.call('save',json.dumps(config))['ret'],'success')
        self.assertEqual(self.scheduler.add_job_instance.call_args.kwargs,{'run':False})
        self.module.manager.start.assert_not_called()

    def test_manual_selected_forwarded_without_limits(self):
        self.assertEqual(self.call('download','42','["1","2","3"]')['ret'],'success')
        mode,conf,args=self.module.manager.start.call_args.args
        self.assertEqual(mode,'download');self.assertEqual(args['selected'],['1','2','3'])

    def test_registered_titles_and_remove_keep_history(self):
        from wolf_downloader.history import History
        h = History(self.module.data_dir() / 'history.db')
        h.save('42', '1', work_title='기존 작품', status='completed')
        h.save('42', '2', work_title='기존 작품', status='failed')
        h.remember_work('43', '분석된 작품')
        self.settings['titles'] = '42\n43\n44'
        rows = self.call('titles')['rows']
        self.assertEqual([r['title'] for r in rows], ['기존 작품', '분석된 작품', '작품명 확인 전'])
        self.assertEqual([r['completed'] for r in rows], [1, 0, 0])
        self.assertEqual(self.call('remove_auto', '42')['ret'], 'success')
        self.assertEqual(self.settings['titles'], '43\n44')
        self.assertEqual(h.get('42', '1')['status'], 'completed')
        self.module.manager.start.assert_not_called()

    def test_run_registered_title_only(self):
        self.settings['titles'] = '42\n43'
        self.assertEqual(self.call('run_title', '44')['ret'], 'error')
        self.module.manager.start.assert_not_called()
        self.assertEqual(self.call('run_title', '43')['ret'], 'success')
        mode, conf, args = self.module.manager.start.call_args.args
        self.assertEqual((mode, args), ('download', {'work_id': '43'}))
        self.assertNotIn('selected', args)

    def test_effective_domain_uses_matching_cache_not_stale_status(self):
        from wolf_downloader.common import write_json
        self.settings['base_url'] = 'https://wfwf100.com'
        write_json(self.module.data_dir() / 'domain.json', {
            'configured_base': 'https://wfwf100.com', 'family': 'wfwf', 'base_url': 'https://wfwf105.com'})
        self.module.manager.status.return_value['base_url'] = 'https://wfwf99.com'
        state = self.call('status')['state']
        self.assertEqual(state['base_url'], 'https://wfwf105.com')
        self.assertTrue(state['domain_state']['verified'])
        html = self.client.get('/wolf_downloader/basic/setting').get_data(as_text=True)
        advanced = html.split('id="panel-advanced"', 1)[1].split('id="panel-files"', 1)[0]
        for key in ('announcer_url', 'current-domain', 'base_url', 'domain_family', 'auto_resolve_base_url'):
            self.assertIn('id="' + key + '"', advanced)
        self.assertIn('https://wfwf105.com', advanced)
        self.settings['base_url'] = 'https://wfwf110.com'
        state = self.call('status')['state']
        self.assertEqual(state['base_url'], 'https://wfwf110.com')
        self.assertFalse(state['domain_state']['verified'])
        self.settings['base_url'] = 'https://wfwf100.com'
        self.settings['domain_family'] = 'wftoon'
        self.assertFalse(self.call('status')['state']['domain_state']['verified'])

    def test_recent_works_limit_order_dedup_and_persistence(self):
        from wolf_downloader.history import History
        h = History(self.module.data_dir() / 'history.db')
        h.save('10', '1', work_title='기존 작품', status='completed')
        self.assertEqual(self.call('recent_works')['rows'], [{'id': '10', 'title': '기존 작품'}])
        for i in range(20, 26):
            h.remember_work(str(i), '작품 ' + str(i), recent=True)
        h.remember_work('20', '다시 분석한 작품', recent=True)
        h.remember_work('99', '목록에서만 조회한 작품')
        # New connection must retain recent analysis even without episode history.
        rows = self.call('recent_works')['rows']
        self.assertEqual([r['id'] for r in rows], ['20', '25', '24', '23', '22'])
        self.assertEqual(rows[0]['title'], '다시 분석한 작품')
        self.module.manager.start.assert_not_called()
        html = self.client.get('/wolf_downloader/basic/manual').get_data(as_text=True)
        self.assertLess(html.index('id="work_url"'), html.index('id="recent-works"'))
        self.assertLess(html.index('id="recent-works"'), html.index('id="analyze"'))

    def test_csrf_required(self):
        r=self.client.post('/wolf_downloader/ajax/basic/command',data={'command':'run'})
        self.assertEqual(r.status_code,403);self.module.manager.start.assert_not_called()

    def test_saved_logs_newest_first_and_bounded(self):
        from wolf_downloader.common import write_log
        self.assertEqual(self.call('logs')['rows'], [])
        write_log(self.module.data_dir(), 'WARNING', '작품 42 경고')
        write_log(self.module.data_dir(), 'ERROR', '<script>오류</script>')
        rows = self.call('logs')['rows']
        self.assertEqual(rows[0]['level'], 'ERROR')
        self.assertEqual(rows[0]['message'], '<script>오류</script>')
        self.assertEqual(rows[1]['level'], 'WARNING')
        path = self.module.data_dir().parent / 'log' / 'wolf_downloader.log'
        path.write_text('긴 로그\n' * 60000, encoding='utf-8')
        self.assertEqual(len(self.call('logs')['rows']), 500)
        self.module.manager.start.assert_not_called()

    def test_separate_registration_moves_membership_without_downloading(self):
        self.settings.update(titles='42', separate_path=self.tmp.name+'/separate')
        self.assertEqual(self.call('add_auto', '42', 'separate')['ret'], 'success')
        self.assertEqual(self.settings['titles'], '')
        self.assertEqual(self.settings['separate_titles'], '42')
        self.assertEqual(self.call('titles', 'separate')['rows'][0]['id'], '42')
        self.assertEqual(self.call('add_auto', '42')['ret'], 'success')
        self.assertEqual(self.settings['separate_titles'], '')
        self.module.manager.start.assert_not_called()

    def test_manual_separate_flag_and_missing_path(self):
        data = {'command':'download', 'arg1':'42', 'arg2':'["3"]', 'csrf_token':'token', 'separate':'true'}
        url = '/wolf_downloader/ajax/basic/command'
        self.assertEqual(self.client.post(url, data=data).get_json()['ret'], 'error')
        self.module.manager.start.assert_not_called()
        self.settings['separate_path'] = self.tmp.name+'/separate'
        self.assertEqual(self.client.post(url, data=data).get_json()['ret'], 'success')
        self.assertEqual(self.module.manager.start.call_args.args[2], {'work_id':'42', 'selected':['3'], 'separate':True})

    def test_secret_not_in_rendered_page(self):
        conf=dict(self.module.config(),base_url='https://wfwf100.com',cookie='very-secret-value=1')
        self.assertEqual(self.call('save',json.dumps(conf))['ret'],'success')
        page=self.client.get('/wolf_downloader/basic/setting').get_data(as_text=True)
        self.assertNotIn('very-secret-value',page)
        self.assertEqual(self.module.runtime_config()['cookie'],'very-secret-value=1')

    def test_bad_selected_input_rejected(self):
        self.assertEqual(self.call('download','42','["../escape"]')['ret'],'error')
        self.module.manager.start.assert_not_called()

    def test_invalid_webhook_rejected(self):
        conf=dict(self.module.config(),notify_webhook_alert='http://127.0.0.1/secret')
        self.assertEqual(self.call('save',json.dumps(conf))['ret'],'error')


if __name__=='__main__':unittest.main()


