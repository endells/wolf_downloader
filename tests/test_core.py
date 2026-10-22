import copy
import io
import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
from common import DEFAULTS, WolfError, Cancelled, Blocked, validate, write_json, read_json, safe_name, inside
from client import parse_work, parse_images, parse_catalog, Client, SafeRedirect, image_extension
from domains import announcements, resolve
from engine import Engine
from history import History
from wolf_downloader.manager import Manager

PNG = b'\x89PNG\r\n\x1a\n' + b'test data' * 20
WORK = {'id': '42', 'title': '시험 작품', 'description': '설명', 'cover': '', 'episodes': [
    {'id': str(i), 'no': i, 'title': '{}화'.format(i)} for i in range(1, 4)]}


class FakeClient:
    def __init__(self, config):
        self.config, self.base, self.downloads = config, 'https://wfwf100.com', []
        self.cancelled = False
        self.fail = False
        self.block = False

    def check(self):
        if self.cancelled:
            raise Cancelled('cancelled')

    def wait(self, value):
        self.check()

    def work(self, wid):
        return copy.deepcopy(dict(WORK, id=wid))

    def html(self, url):
        return '<div id="viewer"><img data-src="/a.png"><img data-src="/b.png"></div>', url

    def image(self, url, target, referer):
        self.check()
        if self.block:
            raise Blocked('blocked')
        if self.fail and url.endswith('b.png'):
            raise WolfError('failed second image')
        path = target.with_suffix('.png')
        path.write_bytes(PNG)
        self.downloads.append(url)
        return path, len(PNG)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conf = validate(dict(DEFAULTS, download_path=str(self.root / 'downloads'), base_url='https://wfwf100.com', titles='42', max_per_run=1))
        self.job = {'id': 'test', 'mode': 'download', 'config': self.conf, 'args': {}}
        self.client = FakeClient(self.conf)
        self.engine = Engine(self.root, self.job, client=self.client)
        self.engine.connect = lambda force=False: self.client.base

    def tearDown(self):
        self.tmp.cleanup()

    def make_archive(self, name, images=None):
        folder = self.engine.work_dir(WORK)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        with zipfile.ZipFile(path, 'w') as archive:
            for index, data in enumerate(images or [PNG, PNG + b'2'], 1):
                archive.writestr(str(index) + '.png', data)
        return path

    def test_clean_archive_name(self):
        ep = {'id': '187', 'no': 187, 'title': '187 화산귀환 181화 2026-09-09 [187]'}
        self.assertEqual(self.engine.archive_name(ep, 147), '0187_화산귀환 181화 #147.zip')
        ep['title'] = '화산귀환 181화#77'
        self.assertEqual(self.engine.archive_name(ep, 147), '0187_화산귀환 181화 #147.zip')

    def test_new_zip_has_actual_page_count(self):
        self.conf['use_compress'] = True
        self.engine.run()
        self.assertEqual(Path(self.engine.history.get('42', '3')['path']).name, '0003_3화 #2.zip')

    def test_reconcile_adds_count_and_cleans_old_name(self):
        old = self.make_archive('0003_3 시험 작품 177화 2026-09-09 [3].zip')
        self.job['mode'] = 'reconcile'
        self.engine.run()
        target = old.with_name('0003_시험 작품 177화 #2.zip')
        self.assertTrue(target.exists())
        self.assertFalse(old.exists())
        self.assertEqual(self.engine.history.get('42', '3')['path'], str(target))

    def test_reconcile_prefers_tagged_and_preserves_other_prefix(self):
        plain = self.make_archive('0003_시험 작품 177화.zip')
        tagged = self.make_archive('0003_시험 작품 177화#2.zip')
        other = self.make_archive('0002_시험 작품 176화.zip')
        self.job['mode'] = 'reconcile'
        self.engine.run()
        target = tagged.with_name('0003_시험 작품 177화 #2.zip')
        self.assertTrue(target.exists())
        self.assertFalse(plain.exists())
        self.assertTrue((target.parent / '.duplicates' / plain.name).exists())
        self.assertTrue(other.with_name('0002_시험 작품 176화 #2.zip').exists())
        self.engine.run()
        self.assertEqual(len(list((target.parent / '.duplicates').iterdir())), 1)

    def test_same_prefix_different_images_not_removed(self):
        plain = self.make_archive('0003_시험 작품 177화.zip', [PNG + b'other', PNG])
        tagged = self.make_archive('0003_시험 작품 177화#2.zip')
        self.job['mode'] = 'reconcile'
        self.engine.run()
        self.assertTrue(plain.with_name('0003_시험 작품 177화 [보존-2] #2.zip').exists())
        self.assertTrue(tagged.with_name('0003_시험 작품 177화 #2.zip').exists())
        self.assertFalse((plain.parent / '.duplicates').exists())

    def test_duplicate_archives_use_own_counts_without_site_or_decompression(self):
        plain = self.make_archive('0003_국정원 말단직원 29화.zip', [PNG] * 3)
        tagged = self.make_archive('0003_국정원 말단직원 29화#95.zip', [PNG] * 4)
        self.job['mode'] = 'reconcile'
        with patch.object(self.client, 'html', side_effect=AssertionError('viewer')), patch.object(self.engine, 'archive_signature', side_effect=AssertionError('unneeded hash')):
            self.engine.run()
        self.assertTrue(plain.with_name('0003_국정원 말단직원 29화 #3.zip').exists())
        self.assertTrue(tagged.with_name('0003_국정원 말단직원 29화 #4.zip').exists())
        self.assertFalse((plain.parent / '.duplicates').exists())
        self.assertFalse(any('이미지 수가 맞지 않아' in w for w in self.engine.state['warnings']))

    def test_duplicate_equal_archives_normalize_despite_viewer_mismatch(self):
        plain = self.make_archive('0003_국정원 말단직원 29화.zip', [PNG] * 3)
        tagged = self.make_archive('0003_국정원 말단직원 29화#95.zip', [PNG] * 3)
        self.job['mode'] = 'reconcile'
        with patch.object(self.client, 'html', side_effect=AssertionError('viewer')):
            self.engine.run()
        self.assertTrue(tagged.with_name('0003_국정원 말단직원 29화 #3.zip').exists())
        self.assertTrue((plain.parent / '.duplicates' / plain.name).exists())
        self.assertEqual(self.engine.history.get('42', '3')['pages'], 3)

    def test_conflicting_content_names_are_stable_on_second_run(self):
        self.make_archive('0003_시험 작품 177화.zip', [PNG + b'other', PNG])
        old = self.make_archive('0003_시험 작품 177화#2.zip')
        self.job['mode'] = 'reconcile'
        self.engine.run()
        first = sorted((p.name, p.read_bytes()) for p in old.parent.glob('*.zip'))
        self.engine.run()
        self.assertEqual(first, sorted((p.name, p.read_bytes()) for p in old.parent.glob('*.zip')))
        self.assertEqual(len(first), 2)

    def test_duplicate_retention_expiry_removes_only_backup_folder(self):
        plain = self.make_archive('0003_시험 작품 3화.zip')
        self.make_archive('0003_시험 작품 3화#2.zip')
        self.job['mode'] = 'reconcile'
        self.engine.run()
        backup = plain.parent / '.duplicates'
        ledger = read_json(self.root / 'duplicate-retention.json')
        since = next(iter(ledger.values()))['since']
        lifetime = self.conf['backup_retention_days'] * 86400
        with patch('engine.time.time', return_value=since + lifetime - 1):
            self.engine.cleanup_duplicates()
        self.assertTrue(backup.exists())
        with patch('engine.time.time', return_value=since + lifetime + 1):
            self.engine.cleanup_duplicates()
        self.assertFalse(backup.exists())
        self.assertTrue(plain.parent.is_dir())
        self.assertTrue(plain.with_name('0003_시험 작품 3화 #2.zip').exists())

    def test_legacy_retention_starts_now_and_never_deletes_last_copy(self):
        path = self.make_archive('0003_시험 작품 3화.zip')
        backup = path.parent / '.duplicates'
        backup.mkdir()
        path.rename(backup / path.name)
        old = backup / path.name
        os.utime(old, (1, 1))
        self.engine.cleanup_duplicates()
        self.assertTrue(old.exists())
        with patch('engine.time.time', return_value=time.time() + (self.conf['backup_retention_days'] + 1) * 86400):
            self.engine.cleanup_duplicates()
        self.assertTrue(old.exists())

    def test_duplicate_cleanup_scope_unknown_files_and_links(self):
        folder = self.engine.work_dir(WORK)
        folder.mkdir(parents=True)
        backup = folder / '.duplicates'
        backup.mkdir()
        (backup / 'other.txt').write_text('keep')
        outside = self.root / 'other'
        outside.mkdir()
        (outside / 'file.zip').write_text('keep')
        (backup / '0003_link.zip').symlink_to(outside / 'file.zip')
        unregistered = folder.parent / 'Other [99]' / '.duplicates'
        unregistered.mkdir(parents=True)
        self.engine.cleanup_duplicates()
        self.assertTrue(backup.exists())
        self.assertTrue((backup / 'other.txt').exists())
        self.assertTrue((outside / 'file.zip').exists())
        self.assertTrue(unregistered.exists())
        (backup / 'other.txt').unlink()
        (backup / '0003_link.zip').unlink()
        self.engine.cleanup_duplicates()
        self.assertFalse(backup.exists())
        self.assertTrue(folder.exists())

    def test_changed_backup_restarts_retention(self):
        plain = self.make_archive('0003_시험 작품 3화.zip')
        self.make_archive('0003_시험 작품 3화#2.zip')
        self.job['mode'] = 'reconcile'
        self.engine.run()
        path = plain.parent / '.duplicates' / plain.name
        with zipfile.ZipFile(path, 'a') as archive:
            archive.writestr('note.txt', 'modified')
        future = time.time() + (self.conf['backup_retention_days'] + 1) * 86400
        with patch('engine.time.time', return_value=future):
            self.engine.cleanup_duplicates()
        self.assertTrue(path.exists())
        self.assertEqual(read_json(self.root / 'duplicate-retention.json')[str(path)]['since'], future)

    def test_different_content_backups_expire_but_primary_survives(self):
        self.make_archive('0003_시험 작품 3화.zip', [PNG + b'other', PNG])
        self.make_archive('0003_시험 작품 3화#2.zip')
        self.job['mode'] = 'reconcile'
        self.engine.run()
        primary = Path(self.engine.history.get('42', '3')['path'])
        before = primary.read_bytes()
        backups = [p for p in primary.parent.glob('*.zip') if p != primary]
        self.assertEqual(len(backups), 1)
        with patch('engine.time.time', return_value=time.time() + 8 * 86400):
            self.engine.cleanup_duplicates()
        self.assertEqual(primary.read_bytes(), before)
        self.assertFalse(backups[0].exists())

    def test_different_page_counts_retained_in_ledger_expire(self):
        self.make_archive('0003_시험 작품 3화.zip', [PNG] * 3)
        self.make_archive('0003_시험 작품 3화#2.zip')
        self.job['mode'] = 'reconcile'
        self.engine.run()
        primary = Path(self.engine.history.get('42', '3')['path'])
        with patch('engine.time.time', return_value=time.time() + 8 * 86400):
            self.engine.cleanup_duplicates()
        self.assertEqual(list(primary.parent.glob('*.zip')), [primary])

    def test_retention_disabled_and_history_primary_preserved(self):
        self.make_archive('0003_시험 작품 3화.zip', [PNG + b'other', PNG])
        self.make_archive('0003_시험 작품 3화#2.zip')
        self.job['mode'] = 'reconcile'
        self.engine.run()
        folder = self.engine.work_dir(WORK)
        paths = list(folder.glob('*.zip'))
        self.conf['backup_retention_days'] = 0
        with patch('engine.time.time', return_value=time.time() + 100 * 86400):
            self.engine.cleanup_duplicates()
        self.assertTrue(all(p.exists() for p in paths))
        # Even a [보존-N] file becomes protected if history selects it as primary.
        alternative = next(p for p in paths if '[보존-' in p.name)
        self.engine.history.save('42', '3', path=str(alternative))
        self.conf['backup_retention_days'] = 1
        with patch('engine.time.time', return_value=time.time() + 100 * 86400):
            self.engine.cleanup_duplicates()
        self.assertTrue(alternative.exists())

    def test_retention_days_validation(self):
        self.assertEqual(validate(dict(self.conf, backup_retention_days='0'))['backup_retention_days'], 0)
        with self.assertRaises(WolfError):
            validate(dict(self.conf, backup_retention_days=-1))

    def test_reconcile_completion_notification_with_counts(self):
        self.make_archive('0003_시험 작품 3화.zip')
        self.job['mode'] = 'reconcile'
        with patch.object(self.engine, 'notify') as notify:
            self.engine.run()
        notify.assert_called_once()
        kind, message = notify.call_args.args
        self.assertEqual(kind, 'download')
        self.assertIn('기존 파일 이력 반영 완료', message)
        self.assertIn('이력 반영: 1회차', message)
        self.assertIn('소요 시간:', message)

    def test_reconcile_empty_and_partial_failure_notify(self):
        self.job['mode'] = 'reconcile'
        with patch.object(self.engine, 'notify') as notify:
            self.engine.run()
        self.assertIn('이력 반영: 0회차', notify.call_args.args[1])
        with patch.object(self.client, 'work', side_effect=WolfError('failure')), patch.object(self.engine, 'notify') as notify:
            self.engine.run()
        self.assertIn('실패: 1건', notify.call_args.args[1])

    def test_reconcile_cancel_does_not_notify_completion(self):
        self.job['mode'] = 'reconcile'
        with patch.object(self.client, 'work', side_effect=Cancelled('stop')), patch.object(self.engine, 'notify') as notify:
            with self.assertRaises(Cancelled):
                self.engine.run()
        notify.assert_not_called()

    def test_domain_change_notifies_old_and_new_once(self):
        with patch('engine.resolve', return_value='https://wfwf101.com'), patch.object(self.engine, 'notify') as notify:
            Engine.connect(self.engine)
            Engine.connect(self.engine)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], 'alert')
        self.assertIn('이전 주소: https://wfwf100.com', notify.call_args.args[1])
        self.assertIn('새 주소: https://wfwf101.com', notify.call_args.args[1])

    def test_same_domain_and_failed_resolution_do_not_notify(self):
        with patch('engine.resolve', return_value=self.client.base), patch.object(self.engine, 'notify') as notify:
            Engine.connect(self.engine, force=True)
        notify.assert_not_called()
        with patch('engine.resolve', side_effect=WolfError('unavailable')), patch.object(self.engine, 'notify') as notify:
            with self.assertRaises(WolfError):
                Engine.connect(self.engine, force=True)
        notify.assert_not_called()

    def test_zero_byte_zip_repaired_even_with_download_limit(self):
        path = self.make_archive('0003_3화.zip')
        path.write_bytes(b'')
        self.job['mode'] = 'reconcile'
        self.engine.run()
        self.assertEqual(self.engine.state['repaired'], 1)
        self.assertTrue(path.with_name('0003_3화 #2.zip').exists())
        self.assertFalse(path.exists())
        self.assertFalse(self.conf['use_compress'])

    def test_repair_failure_retains_broken_file(self):
        path = self.make_archive('0003_3화.zip')
        path.write_bytes(b'')
        self.client.fail = True
        self.job['mode'] = 'reconcile'
        self.engine.run()
        self.assertTrue(path.exists())
        self.assertEqual(self.engine.history.get('42', '3')['status'], 'failed')

    def test_repair_rejects_conflicting_chapter(self):
        path = self.make_archive('0003_다른 작품 9화.zip')
        path.write_bytes(b'')
        self.job['mode'] = 'reconcile'
        self.engine.run()
        self.assertTrue(path.exists())
        self.assertEqual(self.client.downloads, [])

    def test_repair_rejects_neighbor_number_conflict(self):
        path = self.make_archive('0003_3화.zip')
        path.write_bytes(b'')
        self.make_archive('0002_8화.zip')
        self.job['mode'] = 'reconcile'
        self.engine.run()
        self.assertTrue(path.exists())
        self.assertEqual(self.client.downloads, [])

    def test_repair_allows_prefix_and_chapter_offset(self):
        path = self.make_archive('0003_작품 177화.zip')
        path.write_bytes(b'')
        work = copy.deepcopy(WORK)
        work['episodes'][2]['title'] = '작품 177화'
        self.job['mode'] = 'reconcile'
        with patch.object(self.client, 'work', return_value=work):
            self.engine.run()
        self.assertTrue(path.with_name('0003_작품 177화 #2.zip').exists())
        self.assertEqual(self.engine.state['repaired'], 1)

    def test_repair_missing_title_uses_both_neighbors(self):
        self.make_archive('0001_1화.zip')
        broken = self.make_archive('0002_2화.zip')
        broken.write_bytes(b'')
        self.make_archive('0003_3화.zip')
        work = copy.deepcopy(WORK)
        work['episodes'][1]['title'] = '회차 정보'
        self.job['mode'] = 'reconcile'
        with patch.object(self.client, 'work', return_value=work):
            self.engine.run()
        self.assertEqual(self.engine.state['repaired'], 1)
        self.assertTrue(broken.with_name('0002_2화 #2.zip').exists())

    def test_repair_failure_shows_actual_reason(self):
        broken = self.make_archive('0003_3화.zip')
        broken.write_bytes(b'')
        self.job['mode'] = 'reconcile'
        with patch.object(self.engine, 'download_episode', side_effect=WolfError('본문 이미지를 찾지 못했습니다.')):
            self.engine.run()
        self.assertIn('본문 이미지를 찾지 못했습니다.', self.engine.history.get('42', '3')['error'])

    def test_work_prefers_numbered_text_over_tooltip(self):
        work, _ = parse_work('<h1>시험 작품</h1><a href="/view?toon=42&num=7" title="보기">257 시험 작품 256화</a>', 'https://wfwf100.com/list?toon=42', '42')
        self.assertEqual(work['episodes'][0]['no'], 257)
        self.assertIn('256화', work['episodes'][0]['title'])

    def test_repair_preserves_existing_target_and_promotes_new_zip(self):
        broken = self.make_archive('0003_3화.zip')
        broken.write_bytes(b'')
        target = self.make_archive('0003_3화 #2.zip', [PNG + b'old', PNG])
        original = target.read_bytes()
        self.job['mode'] = 'reconcile'
        self.engine.run()
        self.assertEqual(self.engine.state['repaired'], 1)
        self.assertFalse(broken.exists())
        self.assertTrue(target.exists())
        self.assertEqual((target.parent / '.duplicates' / target.name).read_bytes(), original)
        self.assertEqual(self.engine.history.get('42', '3')['path'], str(target))

    def test_repair_promotion_failure_restores_existing_target(self):
        broken = self.make_archive('0003_3화.zip')
        broken.write_bytes(b'')
        target = self.make_archive('0003_3화 #2.zip')
        original = target.read_bytes()
        replace = os.replace
        def fail_promotion(src, dst):
            if Path(dst) == target:
                raise OSError(5, 'test error')
            return replace(src, dst)
        self.job['mode'] = 'reconcile'
        with patch('engine.os.replace', side_effect=fail_promotion):
            self.engine.run()
        self.assertEqual(target.read_bytes(), original)
        self.assertTrue(broken.exists())

    def test_repair_error_notification_has_detailed_reason(self):
        broken = self.make_archive('0003_3화.zip')
        broken.write_bytes(b'')
        self.job['mode'] = 'reconcile'
        with patch.object(self.engine, 'download_episode', side_effect=WolfError('본문 이미지 없음')), patch.object(self.engine, 'notify') as notify:
            self.engine.run()
        alerts = [call.args[1] for call in notify.call_args_list if call.args[0] == 'alert']
        self.assertEqual(len(alerts), 1)
        self.assertIn('본문 이미지 없음', alerts[0])
        self.assertIn('작품: 시험 작품', alerts[0])
        self.assertIn('회차: 3화', alerts[0])

    def test_badge_tooltip_does_not_override_overall_index(self):
        work, _ = parse_work('<h1>갓 오브 블랙필드</h1><a href="/view?toon=4579&num=9" title="UP 갓 오브 블랙필드 4부 59화">339 갓 오브 블랙필드 4부 59화 2026-10-17</a>', 'https://wfwf100.com/list?toon=4579', '4579')
        self.assertEqual(work['episodes'][0]['no'], 339)
        self.assertEqual(self.engine.episode_name(work['episodes'][0]), '0339_갓 오브 블랙필드 4부 59화')

    def test_episode_row_index_precedes_season_chapter(self):
        work, _ = parse_work('<h1>갓 오브 블랙필드</h1><table><tr><td>339</td><td><a href="/view?toon=4579&num=9">UP 갓 오브 블랙필드 4부 59화</a></td></tr></table>', 'https://wfwf100.com/list?toon=4579', '4579')
        self.assertEqual(work['episodes'][0]['no'], 339)
        self.assertNotIn('UP', self.engine.episode_name(work['episodes'][0]))

    def test_existing_wrong_badge_name_gets_correct_overall_index(self):
        old = self.make_archive('0059_UP 갓 오브 블랙필드 4부 59화 #2.zip')
        work = dict(WORK, episodes=[{'id': '9', 'no': 339, 'title': '339 갓 오브 블랙필드 4부 59화 2026-10-17'}])
        self.engine.reconcile(work, old.parent)
        self.assertFalse(old.exists())
        self.assertTrue(old.with_name('0339_갓 오브 블랙필드 4부 59화 #2.zip').exists())

    def test_wrong_hash_count_corrected_from_images(self):
        old = self.make_archive('0003_시험 작품 177화#77.zip')
        self.job['mode'] = 'reconcile'
        self.engine.run()
        self.assertTrue(old.with_name('0003_시험 작품 177화 #2.zip').exists())

    def test_maintenance_delay_does_not_change_download_setting(self):
        self.conf['request_delay'] = 5
        job = dict(self.job, mode='reconcile')
        client = FakeClient(self.conf)
        Engine(self.root, job, client=client)
        self.assertEqual(client.config['request_delay'], .5)
        self.assertEqual(self.conf['request_delay'], 5)
        download = Engine(self.root, self.job, client=FakeClient(self.conf))
        self.assertEqual(download.client.config['request_delay'], 5)

    def test_reconcile_cache_avoids_viewer_requests_and_zip_reads(self):
        self.make_archive('0003_시험 작품 3화.zip')
        self.job['mode'] = 'reconcile'
        self.engine.run()
        with patch.object(self.client, 'html') as html, patch.object(self.engine, 'archive_signature') as signature:
            self.engine.run()
        html.assert_not_called()
        signature.assert_not_called()
        self.assertTrue(self.engine.complete(self.engine.history.get('42', '3')))

    def test_changed_zip_invalidates_reconcile_cache(self):
        self.make_archive('0003_시험 작품 3화.zip')
        self.job['mode'] = 'reconcile'
        self.engine.run()
        target = Path(self.engine.history.get('42', '3')['path'])
        with zipfile.ZipFile(target, 'a') as archive:
            archive.writestr('note.txt', 'changed')
        with patch.object(self.client, 'html', wraps=self.client.html) as html:
            self.engine.run()
        html.assert_not_called()

    def test_unchanged_cache_has_no_daily_expiration(self):
        self.make_archive('0003_시험 작품 3화.zip')
        self.job['mode'] = 'reconcile'
        self.engine.run()
        cache_path = self.root / 'reconcile-cache-42.json'
        cache = read_json(cache_path)
        cache['episodes']['3']['time'] = 0
        write_json(cache_path, cache)
        with patch.object(self.client, 'html', wraps=self.client.html) as html:
            self.engine.run()
        html.assert_not_called()

    def test_first_scan_counts_without_decompressing_or_viewer(self):
        old = self.make_archive('0003_시험 작품 177화#77.zip', [PNG, PNG, PNG])
        self.job['mode'] = 'reconcile'
        with patch.object(self.client, 'html', side_effect=AssertionError('viewer request')), patch.object(zipfile.ZipFile, 'open', side_effect=AssertionError('decompression')):
            self.engine.run()
        self.assertTrue(old.with_name('0003_시험 작품 177화 #3.zip').exists())
        self.assertEqual(self.engine.history.get('42', '3')['pages'], 3)

    def test_cancel_flushes_already_renamed_history(self):
        self.make_archive('0001_시험 작품 1화.zip')
        self.make_archive('0002_시험 작품 2화.zip')
        original = self.engine.archive_page_count
        def cancel_after_count(path):
            if path.name.startswith('0002_'):
                raise Cancelled('stop')
            return original(path)
        with patch.object(self.engine, 'archive_page_count', side_effect=cancel_after_count):
            with self.assertRaises(Cancelled):
                self.engine.reconcile(WORK, self.engine.work_dir(WORK))
        self.assertTrue(Path(self.engine.history.get('42', '1')['path']).exists())
        self.assertIsNone(self.engine.history.get('42', '2'))

    def test_fast_cache_skips_directory_read_and_changed_file_recounts(self):
        self.make_archive('0003_시험 작품 3화.zip')
        self.job['mode'] = 'reconcile'
        self.engine.run()
        with patch.object(self.engine, 'archive_page_count', side_effect=AssertionError('cached')):
            self.engine.run()
        target = Path(self.engine.history.get('42', '3')['path'])
        with zipfile.ZipFile(target, 'a') as archive:
            archive.writestr('extra.png', PNG)
        self.engine.run()
        record = self.engine.history.get('42', '3')
        self.assertEqual(record['pages'], 3)
        self.assertTrue(record['path'].endswith(' #3.zip'))

    def test_invalid_zip_is_repaired(self):
        old = self.make_archive('0003_시험 작품.zip')
        old.write_bytes(b'broken')
        self.job['mode'] = 'reconcile'
        self.engine.run()
        self.assertFalse(old.exists())
        record = self.engine.history.get('42', '3')
        self.assertEqual(record['status'], 'completed')
        self.assertTrue(Path(record['path']).exists())

    def test_zip_signature_reads_each_entry_once(self):
        path = self.make_archive('0003_시험 작품 3화.zip')
        original = zipfile.ZipFile.open
        opened = []
        def tracked(archive, name, *args, **kwargs):
            opened.append(name.filename if isinstance(name, zipfile.ZipInfo) else name)
            return original(archive, name, *args, **kwargs)
        with patch.object(zipfile.ZipFile, 'open', tracked):
            signature = self.engine.archive_signature(path)
        self.assertEqual(len(signature), 2)
        self.assertEqual(opened, ['1.png', '2.png'])

    def test_direct_path_and_automatic_limit(self):
        self.engine.run()
        self.assertFalse((self.root / 'downloads' / 'manhwa').exists())
        row = self.engine.history.get('42', '3')
        self.assertEqual(row['status'], 'completed')
        self.assertIn('/시험 작품 [42]/', row['path'])
        self.assertIsNone(self.engine.history.get('42', '1'))

    def test_download_ignores_plain_title_folder(self):
        folder = self.root / 'downloads' / WORK['title']
        folder.mkdir(parents=True)
        self.engine.run()
        self.assertEqual(Path(self.engine.history.get('42', '3')['path']).parent.name, '시험 작품 [42]')
        self.assertEqual(list(folder.iterdir()), [])

    def test_reuse_legacy_title_folder(self):
        folder = self.root / 'downloads' / 'manhwa' / WORK['title']
        folder.mkdir(parents=True)
        self.assertEqual(self.engine.work_dir(WORK, migrate=True), folder.with_name('시험 작품 [42]'))
        self.assertFalse(folder.exists())

    def test_ambiguous_existing_folders_stop(self):
        for name in ('시험 작품', '시험 작품 [42]'):
            (self.root / 'downloads' / name).mkdir(parents=True)
        with self.assertRaises(WolfError):
            self.engine.work_dir(WORK, migrate=True)

    def test_other_work_marker_not_reused(self):
        folder = self.root / 'downloads' / WORK['title']
        folder.mkdir(parents=True)
        write_json(folder / '.wolf-work.json', {'id': '999'})
        with self.assertRaises(WolfError):
            self.engine.work_dir(WORK, migrate=True)

    def test_reconcile_renames_registered_only_and_updates_history(self):
        plain = self.root / 'downloads' / WORK['title']
        plain.mkdir(parents=True)
        (plain / 'keep.txt').write_text('preserve')
        unrelated = plain.with_name('다른 다운로더 작품')
        unrelated.mkdir()
        self.engine.history.save('42', '99', path=str(plain / 'old.zip'), status='failed')
        self.job['mode'] = 'reconcile'
        self.job['args']['work_id'] = '999'
        with patch.object(self.client, 'work', wraps=self.client.work) as work:
            self.engine.run()
        work.assert_called_once_with('42')
        tagged = plain.with_name('시험 작품 [42]')
        self.assertEqual((tagged / 'keep.txt').read_text(), 'preserve')
        self.assertTrue(unrelated.is_dir())
        self.assertEqual(self.engine.history.get('42', '99')['path'], str(tagged / 'old.zip'))
        self.engine.run()  # Already numbered: repeat safely.
        self.assertTrue(tagged.is_dir())

    def test_migration_collision_preserves_both(self):
        root = self.root / 'downloads'
        for name in ('시험 작품', '시험 작품 [42]'):
            p = root / name
            p.mkdir(parents=True)
            (p / 'keep').write_text(name)
        self.job['mode'] = 'reconcile'
        self.engine.run()
        self.assertEqual(self.engine.state['failed'], 1)
        for name in ('시험 작품', '시험 작품 [42]'):
            self.assertEqual((root / name / 'keep').read_text(), name)

    def test_unregistered_plain_folder_never_migrated(self):
        folder = self.root / 'downloads' / WORK['title']
        folder.mkdir(parents=True)
        self.conf['titles'] = '999'
        with self.assertRaises(WolfError):
            self.engine.work_dir(WORK, migrate=True)
        self.assertTrue(folder.exists())

    def test_old_plain_history_does_not_skip_download(self):
        folder = self.root / 'downloads' / WORK['title'] / 'old'
        folder.mkdir(parents=True)
        (folder / '1.png').write_bytes(PNG)
        self.engine.history.save('42', '3', status='completed', path=str(folder), pages=1)
        self.assertFalse(self.engine.complete(self.engine.history.get('42', '3')))

    def test_redownload_history_shows_actual_stage_not_old_zip(self):
        self.conf['use_compress'] = True
        self.job['args'].update(work_id='42', selected=['3'])
        self.engine.run()
        original = self.client.image
        observed = []
        def image(*args):
            observed.append(self.engine.history.get('42', '3'))
            return original(*args)
        with patch.object(self.client, 'image', side_effect=image):
            self.engine.run()
        self.assertTrue(observed)
        self.assertTrue(all(r['status'] == 'downloading' and r['path'].endswith('/.download-3') for r in observed))
        self.assertTrue(self.engine.history.get('42', '3')['path'].endswith('.zip'))

    def test_site_name_never_creates_directory(self):
        with self.assertRaises(WolfError):
            self.engine.work_dir(dict(WORK, title='늑대닷컴 웹툰'))
        self.assertFalse((self.root / 'downloads').exists())

    def test_notification_manual_title_and_selected_numbers(self):
        self.job['args'].update(work_id='42', selected=['1', '3'])
        with patch.object(self.engine, 'notify') as notify:
            self.engine.run()
        detail = notify.call_args_list[0].args
        self.assertEqual(detail, ('download', '늑대 다운로더 다운로드 완료\n웹툰: 시험 작품\n회차: 1화, 3화'))

    def test_notification_excludes_failed_episodes(self):
        self.job['args'].update(work_id='42', selected=['1', '2'])
        original = self.engine.download_episode
        def download(work, ep, folder):
            if ep['id'] == '2':
                raise WolfError('test failure')
            original(work, ep, folder)
        with patch.object(self.engine, 'download_episode', side_effect=download), patch.object(self.engine, 'notify') as notify:
            self.engine.run()
        self.assertTrue(notify.call_args_list[0].args[1].endswith('회차: 1화'))
        self.assertIn('1건 실패', notify.call_args_list[-1].args[1])

    def test_notification_excludes_previously_completed(self):
        self.conf['max_per_run'] = 0
        self.engine.run()
        with patch.object(self.engine, 'notify') as notify:
            self.engine.run()
        self.assertFalse(any('웹툰:' in call.args[1] for call in notify.call_args_list))

    def test_notification_splits_long_list_without_loss(self):
        episodes = [{'no': i} for i in range(1, 1001)] + [{'no': 1000.5}]
        with patch.object(self.engine, 'notify') as notify:
            self.engine.notify_downloaded(WORK, episodes)
        messages = [call.args[1] for call in notify.call_args_list]
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(m) <= 1700 and '웹툰: 시험 작품' in m for m in messages))
        labels = [s for m in messages for s in m.split('회차: ')[1].split(', ')]
        self.assertEqual(labels, [str(i) + '화' for i in range(1, 1001)] + ['1000.5화'])

    def test_notification_separates_works(self):
        self.conf['titles'] = '42\n43'
        self.client.work = lambda wid: copy.deepcopy(dict(WORK, id=wid, title='알림 테스트 작품 ' + wid))
        with patch.object(self.engine, 'notify') as notify:
            self.engine.run()
        details = [call.args[1] for call in notify.call_args_list if '웹툰:' in call.args[1]]
        self.assertEqual(len(details), 2)
        self.assertIn('작품 42', details[0])
        self.assertIn('작품 43', details[1])

    def test_manual_ignores_limit_and_completed_history(self):
        self.job['args'].update(work_id='42', selected=['1', '2', '3'])
        self.engine.run()
        self.assertEqual(len(self.client.downloads), 6)
        self.engine.run()
        self.assertEqual(len(self.client.downloads), 12)
        for eid in ('1', '2', '3'):
            self.assertEqual(self.engine.history.get('42', eid)['status'], 'completed')

    def test_automatic_skips_completed_files(self):
        self.engine.run()
        self.engine.run()
        self.assertEqual(len(self.client.downloads), 4)
        self.assertEqual(self.engine.history.get('42', '2')['status'], 'completed')

    def test_failed_episode_not_marked_complete_and_resume(self):
        self.client.fail = True
        self.engine.run()
        row = self.engine.history.get('42', '3')
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(len(self.client.downloads), 1)
        self.client.fail = False
        self.engine.run()
        self.assertEqual(len(self.client.downloads), 2)
        self.assertEqual(self.engine.history.get('42', '3')['status'], 'completed')

    def test_zip_contains_all_images_no_recompression(self):
        self.conf['use_compress'] = True
        self.engine.run()
        record = self.engine.history.get('42', '3')
        with zipfile.ZipFile(record['path']) as z:
            self.assertEqual(z.testzip(), None)
            self.assertEqual(len([n for n in z.namelist() if n.endswith('.png')]), 2)
            self.assertTrue(all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist()))
        self.assertFalse(Path(record['path'][:-4]).exists())

    def test_manual_zip_replacement_preserves_valid_archive(self):
        self.conf['use_compress'] = True
        self.job['args'].update(work_id='42', selected=['3'])
        self.engine.run()
        path = Path(self.engine.history.get('42', '3')['path'])
        original = path.read_bytes()
        self.client.fail = True
        self.engine.run()
        self.assertEqual(path.read_bytes(), original)
        self.client.fail = False
        self.engine.run()
        with zipfile.ZipFile(path) as z:
            self.assertIsNone(z.testzip())
        self.assertEqual(self.engine.history.get('42', '3')['status'], 'completed')

    def test_partial_legacy_files_not_marked_completed(self):
        folder = self.root / 'downloads' / 'manhwa' / WORK['title'] / '0003_3화'
        folder.mkdir(parents=True)
        (folder / '001.png').write_bytes(PNG)
        self.job['mode'] = 'reconcile'
        self.engine.run()
        self.assertIsNone(self.engine.history.get('42', '3'))
        folder = folder.parent.with_name('시험 작품 [42]') / folder.name
        (folder / '002.png').write_bytes(PNG)
        self.engine.run()
        self.assertEqual(self.engine.history.get('42', '3')['status'], 'completed')
        self.assertTrue(folder.is_dir())

    def test_analyze_creates_no_downloads(self):
        self.job['mode'] = 'analyze'
        self.job['args']['work_id'] = '42'
        self.engine.run()
        self.assertFalse((self.root / 'downloads').exists())
        self.assertEqual(len(read_json(self.root / 'result.json')['work']['episodes']), 3)

    def test_cancel_and_block_stop_queue(self):
        self.client.cancelled = True
        with self.assertRaises(Cancelled):
            self.engine.run()
        self.client.cancelled = False
        self.client.block = True
        with self.assertRaises(Blocked):
            self.engine.run()
        self.assertEqual(self.engine.history.get('42', '3')['status'], 'failed')

    def test_unknown_selected_episode_is_not_silently_omitted(self):
        self.job['args'].update(work_id='42', selected=['999'])
        self.engine.run()
        self.assertEqual(self.engine.state['failed'], 1)
        self.assertEqual(self.client.downloads, [])

    def test_changed_download_root_does_not_skip(self):
        self.engine.run()
        record = self.engine.history.get('42', '3')
        self.conf['download_path'] = str(self.root / 'elsewhere')
        self.assertFalse(self.engine.complete(record))

    def test_existing_unmanaged_folder_preserved(self):
        work = self.client.work('42')
        target = self.engine.work_dir(work) / self.engine.episode_name(work['episodes'][-1])
        target.mkdir(parents=True)
        (target / 'important.txt').write_text('keep')
        self.engine.run()
        self.assertEqual((target / 'important.txt').read_text(), 'keep')
        self.assertEqual(self.engine.history.get('42', '3')['status'], 'failed')

    def test_compress_only_managed_completed_folder(self):
        self.engine.run()
        folder = self.engine.work_dir(WORK)
        unrelated = folder / 'do-not-touch'
        unrelated.mkdir()
        (unrelated / 'a.png').write_bytes(PNG)
        self.job['mode'] = 'compress'
        self.engine.run()
        self.assertTrue(unrelated.is_dir())
        self.assertTrue(self.engine.history.get('42', '3')['path'].endswith('.zip'))

    def test_atomic_secret_file_mode(self):
        target = self.root / 'secret.json'
        write_json(target, {'cookie': 'secret'})
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_path_escape_symlink_and_bad_values(self):
        with self.assertRaises(WolfError):
            inside(self.root, '../outside')
        (self.root / 'link').symlink_to('/tmp')
        with self.assertRaises(WolfError):
            inside(self.root, 'link', 'oops')
        self.assertEqual(safe_name('a/b:c'), 'a／b：c')
        for key, value in [('download_path', 'relative'), ('bandwidth_mib', 'nan'), ('request_delay', '0'), ('episode_digits', 99)]:
            with self.subTest(key=key), self.assertRaises((WolfError, ValueError)):
                validate(dict(self.conf, **{key: value}))


class LoggingTests(unittest.TestCase):
    def test_persistent_ff_path_redaction_and_newline(self):
        from common import write_log
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / 'wolf_downloader'
            self.assertTrue(write_log(directory, 'INFO', '작품 완료\ncookie=SECRET https://hooks.slack.com/services/secret', ['SECRET']))
            path = Path(temp) / 'log' / 'wolf_downloader.log'
            content = path.read_text()
            self.assertIn('작품 완료', content)
            self.assertNotIn('SECRET', content)
            self.assertNotIn('hooks.slack', content)
            self.assertEqual(len(content.splitlines()), 1)
            write_log(directory, 'WARNING', '재시작 후 기록')
            self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_rotation_keeps_recent_log(self):
        from common import write_log
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / 'wolf_downloader'
            logdir = Path(temp) / 'log'
            logdir.mkdir()
            path = logdir / 'wolf_downloader.log'
            path.write_text('a' * (5 * 1024 * 1024))
            self.assertTrue(write_log(directory, 'INFO', '새 로그'))
            self.assertTrue(path.with_name(path.name + '.1').exists())
            self.assertIn('새 로그', path.read_text())

    def test_log_failure_does_not_abort_work(self):
        from common import write_log
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'log').write_text('blocked')
            self.assertFalse(write_log(Path(temp) / 'wolf_downloader', 'INFO', 'test'))

    def test_worker_events_are_persistent_and_progress_not_spammed(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / 'wolf_downloader'
            directory.mkdir()
            conf = validate(dict(DEFAULTS, download_path=str(Path(temp) / 'downloads'), base_url='https://wfwf100.com', titles='42', max_per_run=1))
            engine = Engine(directory, {'id': 'log-test', 'mode': 'download', 'config': conf, 'args': {}}, client=FakeClient(conf))
            engine.connect = lambda force=False: engine.client.base
            engine.run()
            for page in range(20):
                engine.status(pages_done=page)
            content = (Path(temp) / 'log' / 'wolf_downloader.log').read_text()
            self.assertIn('작업 실행: download', content)
            self.assertIn('다운로드 완료: 시험 작품', content)
            self.assertEqual(content.count('작업 완료: 성공'), 1)


class ParserTests(unittest.TestCase):
    def test_leading_list_number_overrides_story_episode_number(self):
        html = '<h1>화산귀환</h1><a href="/view?toon=42&num=187">187 화산귀환 181화 2026-09-09</a>'
        work, _ = parse_work(html, 'https://example.com/list?toon=42', '42')
        self.assertEqual(work['episodes'][0]['no'], 187)

    def test_generic_metadata_uses_real_heading(self):
        html = '<meta property="og:title" content="늑대닷컴 웹툰"><h1>늑대닷컴</h1><h1>진짜 작품명</h1>'
        work, _ = parse_work(html, 'https://example.com/list?toon=5973', '5973')
        self.assertEqual(work['title'], '진짜 작품명')

    def test_explicit_series_title_overrides_site_metadata(self):
        html = '<meta property="og:title" content="늑대닷컴 웹툰"><div class="toon-title">실제 제목</div>'
        self.assertEqual(parse_work(html, 'https://example.com/list?toon=1', '1')[0]['title'], '실제 제목')

    def test_episode_prefix_recovers_title(self):
        html = '<title>늑대닷컴 웹툰</title><a href="/view?toon=1&num=11">실제 제목 1화</a><a href="/view?toon=1&num=12">실제 제목 2화</a>'
        self.assertEqual(parse_work(html, 'https://example.com/list?toon=1', '1')[0]['title'], '실제 제목')

    def test_unknown_title_stops_client_work(self):
        client = Client(dict(DEFAULTS, base_url='https://example.com'))
        html = '<title>늑대닷컴 웹툰</title><a href="/view?toon=1&num=11">1화</a>'
        with patch.object(client, 'html', return_value=(html, 'https://example.com/list?toon=1')):
            with self.assertRaises(WolfError):
                client.work('1')

    def test_episode_list_title_dedup_order_and_pages(self):
        html = '<meta property="og:title" content="테스트 - 늑대닷컴"><a href="/view?toon=42&num=2">2화</a><a href="/view?toon=42&num=1">1화</a><a href="/view?toon=43&num=1">외부 작품</a><a href="/view?toon=42&num=2">중복</a><a href="/list?toon=42&page=2">다음</a>'
        work, pages = parse_work(html, 'https://wfwf100.com/list?toon=42', '42')
        self.assertEqual(work['title'], '테스트')
        self.assertEqual([e['id'] for e in work['episodes']], ['2', '1'])
        self.assertEqual(len(pages), 1)

    def test_images_dedup_and_exclude_banner(self):
        html = '<aside><img data-src="/ad.jpg"></aside><div id="viewer"><img data-src="/one.jpg"><img data-original="//cdn.example.com/two.png"><img data-src="/one.jpg"></div>'
        self.assertEqual(parse_images(html, 'https://example.com/view', '#viewer'), ['https://example.com/one.jpg', 'https://cdn.example.com/two.png'])
        self.assertEqual(len(parse_images(html, 'https://example.com/view', '#missing')), 2)

    def test_missing_images_error(self):
        with self.assertRaises(WolfError):
            parse_images('<p>empty</p>', 'https://example.com', '#viewer')

    def test_catalog_thumbnail_title_and_pagination(self):
        rows, pages = parse_catalog('<a href="/list?toon=42"><img alt="제목"></a><a href="/ing?page=3">3</a>', 'https://example.com/ing')
        self.assertEqual(rows[0]['title'], '제목')
        self.assertEqual(pages, 3)

    def test_announcement_migration_and_family_filter(self):
        class Fixture:
            def html(self, url):
                if url.endswith('/oldchannel'):
                    return '<a href="https://t.me/s/newchannel">새 채널</a>', url
                return '<a href="https://wfwf101.com">old</a><a href="https://advertisement.com">ad</a><a href="https://wftoon200.com">다른 계열</a><a href="https://wfwf102.com">new</a>', url
        self.assertEqual(announcements(Fixture(), 'https://t.me/s/oldchannel', 'wfwf'), ['https://wfwf102.com', 'https://wfwf101.com'])

    def test_resolver_rejects_dead_candidates_before_update(self):
        c = FakeClient({'base_url': '', 'domain_family': 'wfwf', 'announcer_url': 'https://t.me/s/test', 'auto_resolve_base_url': True})
        c.base = ''
        with patch('domains.announcements', return_value=['https://wfwf1.com','https://wfwf2.com']), patch('domains.verify', side_effect=[WolfError('dead'), 'https://wfwf2.com']):
            self.assertEqual(resolve(c), 'https://wfwf2.com')

    def test_resolver_disabled_does_not_probe_other_domains(self):
        c = FakeClient({'auto_resolve_base_url': False})
        with patch('domains.verify', side_effect=WolfError('dead')), patch('domains.announcements') as ann:
            with self.assertRaises(WolfError): resolve(c)
            ann.assert_not_called()

    def test_redirect_strips_cookie(self):
        req = Request('https://example.com', headers={'Cookie': 'secret=yes'})
        with patch('client.public_url'):
            redirected = SafeRedirect().redirect_request(req, None, 302, '', {}, 'https://other.example/path')
        self.assertIsNone(redirected.get_header('Cookie'))

    def test_cookie_is_scoped_to_saved_origin(self):
        config = {'base_url':'https://wfwf100.com', 'cookie':'secret=yes', 'cookie_origin':'https://wfwf99.com', 'request_delay':0}
        c = Client(config)
        with patch('client.public_url'), patch.object(c.opener, 'open') as op:
            c.request('https://wfwf100.com/ing')
            self.assertIsNone(op.call_args.args[0].get_header('Cookie'))
            config['cookie_origin'] = config['base_url']
            c.request('https://wfwf100.com/ing')
            self.assertEqual(op.call_args.args[0].get_header('Cookie'), 'secret=yes')

    def test_image_signatures(self):
        self.assertEqual(image_extension(PNG), '.png')
        self.assertEqual(image_extension(b'<html>blocked</html>'), '')


class ProcessTests(unittest.TestCase):
    def test_lock_survives_parent_and_rejects_duplicate(self):
        import fcntl
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            manager = Manager(tmp)
            lock = (Path(tmp) / 'worker.lock').open('a+b')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.5)'], pass_fds=(lock.fileno(),))
            lock.close()
            try:
                self.assertTrue(manager.busy())
                with self.assertRaises(Exception):
                    manager.start('analyze', {})
                self.assertTrue(Manager(tmp).busy())
            finally:
                proc.wait(timeout=5)
            self.assertFalse(manager.busy())

    def test_real_child_lifecycle_no_network_and_secret_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = Manager(tmp)
            cfg = validate(dict(DEFAULTS, download_path=tmp))
            started = time.monotonic()
            manager.start('notify_test', cfg)
            self.assertLess(time.monotonic() - started, 1)
            manager.process.wait(timeout=10)
            state = manager.status()
            self.assertEqual(state['status'], 'done')
            self.assertFalse(state['active'])
            self.assertFalse((Path(tmp) / 'job.json').exists())
            self.assertNotEqual(manager.process.pid, os.getpid())

    def test_crash_state_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = Manager(tmp)
            write_json(Path(tmp)/'status.json', {'status':'running'})
            self.assertEqual(manager.status()['status'], 'interrupted')


if __name__ == '__main__':
    unittest.main()

