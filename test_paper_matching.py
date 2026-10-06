"""Run from the CNKIDownload project; all filesystem cases use temporary fixtures."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
PROJECT = Path(os.environ.get('CNKI_PROJECT_DIR', os.getcwd())).resolve()
sys.path.insert(0, str(PROJECT))
import CNKIDownload_cdp as cdp
spec = importlib.util.spec_from_file_location('paper_matching_main', PROJECT/'3.CNKIDownload.py')
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

TITLE_A = '城市轨道交通短期客流预测方法研究'
TITLE_B = '城市轨道交通短期客流预测模型比较'
URL_A = 'https://kns.cnki.net/kcms2/article/abstract?v=paper-a'
URL_B = 'https://kns.cnki.net/kcms2/article/abstract?v=paper-b'

def item(no=1, title=TITLE_A, author='张三', year='2023'):
    return {'no':no, 'title':title, 'author':author, 'year':year, 'journal':''}

def link(title=TITLE_A, href=URL_A, author='', year=''):
    return {'t':title, 'h':href, 'author':author, 'year':year}

class FakeTab:
    def __init__(self, rows):
        self.rows, self.visited = rows, []
    def goto(self, url, **kwargs): self.visited.append(url)
    def url(self): return 'https://kns.cnki.net/kns8s/defaultresult/index'
    def body_text(self, *args): return ''
    def js(self, *args, **kwargs): return json.dumps(self.rows, ensure_ascii=False)

class FilenameTests(unittest.TestCase):
    def test_distinct_titles_with_same_first_ten_characters(self):
        items=[item(), item(2,TITLE_B)]
        self.assertEqual(cdp.match_list_by_filename(TITLE_B+'_张三',items)['no'],2)
    def test_short_title_with_known_author(self):
        self.assertEqual(cdp.match_list_by_filename('机器学习综述_张三',[item(title='机器学习综述')])['no'],1)
    def test_title_only_without_author_metadata(self):
        self.assertEqual(cdp.match_list_by_filename('机器学习综述',[item(title='机器学习综述',author='')])['no'],1)
    def test_truncated_title_is_not_accepted(self):
        self.assertIsNone(cdp.match_list_by_filename(TITLE_A[:10]+'_张三',[item()]))
    def test_same_title_without_author_is_ambiguous(self):
        self.assertIsNone(cdp.match_list_by_filename(TITLE_A,[item(),item(2,author='李四')]))
    def test_same_title_author_disambiguates(self):
        self.assertEqual(cdp.match_list_by_filename(TITLE_A+'_李四',[item(),item(2,author='李四')])['no'],2)
    def test_unknown_author_suffix_is_not_guessed(self):
        self.assertIsNone(cdp.match_list_by_filename(TITLE_A+'_李四',[item()]))
    def test_absent_author_metadata_does_not_strip_arbitrary_suffix(self):
        self.assertIsNone(cdp.match_list_by_filename(TITLE_A+'_扩展研究',[item(author='')]))
    def test_duplicate_download_suffix(self):
        self.assertEqual(cdp.match_list_by_filename(TITLE_A+'_张三 (2)',[item()])['no'],1)
    def test_numeric_title_suffix_not_treated_as_duplicate(self):
        for stem in ['农业研究（2023）','农业研究(1)','农业研究 (2023)']:
            with self.subTest(stem=stem):
                self.assertIsNone(cdp.match_list_by_filename(stem,[item(title='农业研究')]))
    def test_numeric_title_suffix_preserved_for_correct_full_title(self):
        target=item(title='农业研究（2023）')
        self.assertEqual(cdp.match_list_by_filename('农业研究（2023）',[target])['no'],1)
    def test_underscore_inside_title(self):
        it=item(title='模型_A的评价研究')
        self.assertEqual(cdp.match_list_by_filename('模型_A的评价研究_张三',[it])['no'],1)
    def test_scientific_symbols_do_not_collapse_different_titles(self):
        self.assertIsNone(cdp.match_list_by_filename('基于C的算法研究',[item(title='基于C++的算法研究')]))
    def test_normalization_supports_case_width_and_punctuation(self):
        self.assertEqual(cdp.normalize_key('ＡＢＣ：模型_研究'),cdp.normalize_key('abc 模型研究'))

class SearchTests(unittest.TestCase):
    def search(self, rows, target=None):
        return main.find_detail_url(FakeTab(rows),target or item(),SimpleNamespace(search_wait=0,captcha_wait=0))
    def test_unrelated_result_rejected(self):
        self.assertIsNone(self.search([link(title='完全无关的论文')]))
    def test_shared_title_prefix_rejected(self):
        self.assertIsNone(self.search([link(title=TITLE_B)]))
    def test_normalized_complete_title_accepted(self):
        target=item(title='ABC：模型研究')
        hit=self.search([link(title='ＡＢＣ 模型研究')],target)
        self.assertIsNotNone(hit)
        self.assertEqual(hit['h'],URL_A)
    def test_same_title_distinct_urls_ambiguous(self):
        self.assertIsNone(self.search([link(),link(href=URL_B)]))
    def test_same_title_matching_author_and_year_selects_one(self):
        hit=self.search([link(author='李四',year='2022'),link(href=URL_B,author='张三',year='2023')])
        self.assertEqual(hit['h'],URL_B)
    def test_conflicting_author_rejected(self):
        self.assertIsNone(self.search([link(author='张三丰',year='2023')]))
    def test_conflicting_year_rejected(self):
        self.assertIsNone(self.search([link(author='张三',year='2022')]))
    def test_duplicate_urls_with_same_metadata_are_one_result(self):
        self.assertIsNotNone(self.search([link(),link()]))
    def test_duplicate_urls_with_conflicting_metadata_rejected(self):
        self.assertIsNone(self.search([link(author='张三'),link(author='李四')]))
    def test_unsafe_url_rejected(self):
        for url in ['https://cnki.net.evil.example/kcms2/article/abstract', 'javascript:alert(1)',
                    'https://kns.cnki.net/', 'https://kns.cnki.net/kns8s/defaultresult/index']:
            with self.subTest(url=url):self.assertIsNone(self.search([link(href=url)]))
    def test_malformed_search_data_rejected(self):
        for rows in ['oops',{'t':TITLE_A},[None,42],[]]:
            with self.subTest(rows=rows):self.assertIsNone(self.search(rows))
    def test_search_uses_complete_title(self):
        title='城市轨道交通'+('完整研究题目'*12)
        tab=FakeTab([])
        main.find_detail_url(tab,item(title=title),SimpleNamespace(search_wait=0,captcha_wait=0))
        from urllib.parse import parse_qs,urlparse
        self.assertEqual(parse_qs(urlparse(tab.visited[0]).query)['kw'],[title])

class FilesystemTests(unittest.TestCase):
    def setUp(self):
        root=Path(os.environ.get('CNKI_MATCH_TEST_ROOT',tempfile.gettempdir()))
        root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='cnki_matching_',dir=root)
        self.base=Path(self.temp.name)
        self.dl,self.out,self.state=(self.base/x for x in ('Downloads','out','state'))
        for p in (self.dl,self.out,self.state):p.mkdir()
        self.cfg=SimpleNamespace(max_age_h=6,any_age=True,min_kb=20,name_len=25,dry_run=False,
                                 verified_files={},config_file=str(self.state/'cnki_config.json'))
    def tearDown(self): self.temp.cleanup()
    def pdf(self, folder, name):
        f=folder/name
        f.write_bytes(b'%PDF-1.4\n'+b'0'*30000)
        return f
    def write(self,name,data):
        p=self.state/name
        p.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
        return p
    def run_main(self,args,rows=None):
        output=io.StringIO()
        rows=[] if rows is None else rows
        tab=FakeTab(rows)
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(output))
            for name,value in {
                'user_downloads_dir':lambda:str(self.dl),
                'profile_has_cookies':lambda p:True,
                'cdp_status':lambda p:{'ok':True,'version':{'Browser':'offline test'}},
                'list_tabs':lambda p:[{'id':'fixture','url':'https://kns.cnki.net/kns8s/defaultresult'}],
                'Tab':lambda *a:tab,
                'new_tab':lambda *a:(_ for _ in ()).throw(AssertionError('unexpected tab opening')),
            }.items():stack.enter_context(patch.object(main,name,value))
            stack.enter_context(patch.object(main.time,'sleep',return_value=None))
            rc=main.main(['--work',str(self.state),'--out',str(self.out),*args])
        return rc,output.getvalue()
    def test_ambiguous_download_remains_untouched(self):
        source=self.pdf(self.dl,TITLE_A+'.pdf')
        result=main.collect_downloads([item(),item(2,author='李四')],str(self.dl),str(self.out),self.cfg)
        self.assertTrue(source.exists())
        self.assertEqual(result['renamed'],[])
    def test_correct_full_title_download_moved(self):
        source=self.pdf(self.dl,TITLE_B+'_张三.pdf')
        main.collect_downloads([item(),item(2,TITLE_B)],str(self.dl),str(self.out),self.cfg)
        self.assertFalse(source.exists())
        self.assertTrue((self.out/('2_'+TITLE_B+'.pdf')).exists())
    def test_long_title_remains_complete_in_verified_file_record(self):
        target=item(title=TITLE_A+'完整长标题后半部分不可混淆')
        self.pdf(self.dl,target['title']+'_张三.pdf')
        main.collect_downloads([target],str(self.dl),str(self.out),self.cfg)
        stored=json.loads(Path(self.cfg.config_file).read_text(encoding='utf-8'))
        cfg=SimpleNamespace(min_kb=20,verified_files=stored['verifiedFiles'])
        result=main.scan_progress([target],str(self.out),str(self.dl),cfg)
        self.assertEqual(result['done'],[1])
    def test_verified_long_title_persists_through_main_restart(self):
        target=item(title=TITLE_A+'完整长标题后半部分不可混淆')
        self.write('cnki_list.json',[target])
        self.pdf(self.dl,target['title']+'_张三.pdf')
        first,output=self.run_main(['--collect'])
        self.assertEqual(first,0)
        self.assertIn('已完成 1 / 1',output)
        second,output=self.run_main(['--status'])
        self.assertEqual(second,0)
        self.assertIn('已完成 1 / 1',output)
    def test_verified_duplicate_does_not_replace_original(self):
        target=item()
        self.pdf(self.dl,TITLE_A+'_张三.pdf')
        main.collect_downloads([target],str(self.dl),str(self.out),self.cfg)
        source=self.pdf(self.dl,TITLE_A+'_张三 (2).pdf')
        result=main.collect_downloads([target],str(self.dl),str(self.out),self.cfg)
        self.assertFalse(source.exists())
        self.assertEqual(len(result['duplicates']),1)
        self.assertTrue((self.out/'_重复副本'/(TITLE_A+'_张三 (2).pdf')).exists())
    def test_empty_or_wrong_title_numbered_files_not_completed(self):
        (self.out/'1_wrong.pdf').write_bytes(b'')
        self.pdf(self.out,'999_other.pdf')
        self.assertEqual(main.scan_progress([item()],str(self.out),str(self.dl),self.cfg)['done'],[])
    def test_existing_full_title_file_accepted_without_record(self):
        self.pdf(self.out,'1_'+TITLE_A+'.pdf')
        self.assertEqual(main.scan_progress([item()],str(self.out),str(self.dl),self.cfg)['done'],[1])
    def test_old_truncated_title_not_assumed_completed(self):
        target=item(title=TITLE_A+'完整长标题后半部分不可混淆')
        self.pdf(self.out,'1_'+target['title'][:25]+'.pdf')
        result=main.scan_progress([target],str(self.out),str(self.dl),self.cfg)
        self.assertEqual(result['done'],[])
        self.assertTrue(result['needs_review'])
    def test_changed_title_invalidates_verified_record(self):
        title=TITLE_A+'完整長标题后半部分甲'
        target=item(title=title)
        self.pdf(self.dl,title+'_张三.pdf')
        main.collect_downloads([target],str(self.dl),str(self.out),self.cfg)
        changed=item(title=TITLE_A+'完整長标题后半部分乙')
        self.assertEqual(main.scan_progress([changed],str(self.out),str(self.dl),self.cfg)['done'],[])
    def test_replaced_file_invalidates_verified_record(self):
        target=item(title=TITLE_A+'完整长标题后半部分不可混淆')
        self.pdf(self.dl,target['title']+'_张三.pdf')
        main.collect_downloads([target],str(self.dl),str(self.out),self.cfg)
        f=next(self.out.glob('*.pdf'))
        f.write_bytes(b'%PDF-1.4\n'+b'1'*30000)
        self.assertEqual(main.scan_progress([target],str(self.out),str(self.dl),self.cfg)['done'],[])
    def test_unverified_existing_target_does_not_swallow_new_correct_pdf(self):
        target=item(title=TITLE_A+'完整长标题后半部分不可混淆')
        existing=self.pdf(self.out,'1_'+target['title'][:25]+'.pdf')
        source=self.pdf(self.dl,target['title']+'_张三.pdf')
        result=main.collect_downloads([target],str(self.dl),str(self.out),self.cfg)
        self.assertTrue(source.exists())
        self.assertEqual(existing.read_bytes(),b'%PDF-1.4\n'+b'0'*30000)
        self.assertTrue(result['errors'])
    def test_explicit_new_list_overrides_previous_work_list(self):
        self.write('cnki_list.json',[item()])
        newer=self.base/'new.json'
        newer.write_text(json.dumps([item(2,TITLE_B)],ensure_ascii=False),encoding='utf-8')
        rc,output=self.run_main([str(newer),'--status'])
        self.assertEqual(rc,0)
        self.assertIn(TITLE_B,output)
        self.assertNotIn(TITLE_A,output)
    def test_duplicate_ids_rejected_before_collecting(self):
        self.write('cnki_list.json',[item(),item(1,TITLE_B)])
        source=self.pdf(self.dl,TITLE_A+'_张三.pdf')
        rc,_=self.run_main(['--status'])
        self.assertNotEqual(rc,0)
        self.assertTrue(source.exists())
    def test_legacy_url_cache_is_researched(self):
        self.write('cnki_list.json',[item()])
        self.write('cnki_detail_urls.json',{'1':{'href':URL_B,'title':TITLE_A,'hit':TITLE_A}})
        rc,_=self.run_main(['--no-launch','--no-open'],[link(href=URL_A)])
        self.assertEqual(rc,0)
        cache=json.loads((self.state/'cnki_detail_urls.json').read_text(encoding='utf-8'))
        self.assertEqual(cache['1']['href'],URL_A)
    def test_changed_author_invalidates_cache(self):
        self.write('cnki_list.json',[item()])
        self.run_main(['--no-launch','--no-open'],[link(author='张三',year='2023')])
        self.write('cnki_list.json',[item(author='李四')])
        rc,_=self.run_main(['--no-launch','--no-open'],[link(href=URL_B,author='李四',year='2023')])
        self.assertEqual(rc,0)
        cache=json.loads((self.state/'cnki_detail_urls.json').read_text(encoding='utf-8'))
        self.assertEqual(cache['1']['href'],URL_B)
    def test_verified_cache_can_be_reused(self):
        self.write('cnki_list.json',[item()])
        self.run_main(['--no-launch','--no-open'],[link()])
        with patch.object(main,'find_detail_url',side_effect=AssertionError('cached paper was searched')):
            rc,_=self.run_main(['--no-launch','--no-open'])
        self.assertEqual(rc,0)
    def test_stale_cache_not_reported_as_located_in_offline_mode(self):
        self.write('cnki_list.json',[item()])
        self.write('cnki_detail_urls.json',{'1':{'href':URL_B,'title':TITLE_B,'hit':TITLE_B}})
        rc,output=self.run_main(['--status'])
        self.assertEqual(rc,0)
        self.assertNotIn('[已定位',output)

if __name__=='__main__':unittest.main(verbosity=2)
