"""Offscreen Qt integration checks. Install requirements to enable these."""
import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
HAS_QT = importlib.util.find_spec('PySide6') is not None

@unittest.skipUnless(HAS_QT,'PySide6 is not installed')
class Desktop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        font=self.app.font(); font.setPointSize(9); self.app.setFont(font)
    def test_forms_tabs_charts_and_history(self):
        from promptnik.storage import Store
        from promptnik.ui import Window, SessionTab, Connections, DecisionResult
        with tempfile.TemporaryDirectory() as root, patch('promptnik.core.discover',return_value={}), patch('promptnik.core.environment_connections',return_value={}):
            store=Store(root); store.config['connections']={'local':{'url':'http://localhost:11434/v1','protocol':'chat','model':'test'}}
            with patch.object(store,'connections',return_value=store.config['connections']):
                w=Window(store); w.show(); self.app.processEvents()
                manager=Connections(w); self.assertEqual(manager.list.count(),1); manager.close()
                w.new_session('chat','local'); chat=w.tabs.currentWidget(); self.assertIsInstance(chat,SessionTab)
                chat.composer.setPlainText('draft'); chat.save_draft()
                w.new_session('decision','local'); decision=w.tabs.currentWidget(); self.assertEqual(len(decision.questions),1)
                question=decision.questions[0]; question.kind.setCurrentIndex(1); question.instructions.setPlainText('Which?'); question.add_option(['a','First']); question.add_option(['b','Second']); decision.state.setPlainText('State'); decision.save_draft()
                self.assertEqual(len(store.list_sessions()),2)
                decision.restore(decision.form_value()); self.assertEqual(decision.questions[0].table.rowCount(),2)
                raw={'answers':{'q':{'type':'choice','choice':'a','probabilities':{'a':.8,'b':.2},'confidence':.6}}}
                chart=DecisionResult(raw); chart.resize(650,400); chart.show(); self.app.processEvents(); self.assertFalse(chart.grab().isNull()); chart.close()
                w.toggle_history(); self.assertFalse(w.sidebar.isVisible()); w.toggle_history()
                w.delete_ids([decision.session['id']]); self.assertEqual(len(store.list_sessions()),1)
                self.assertEqual(chat.composer.toPlainText(),'draft')
                w.close(); self.app.processEvents(); store.db.close()
    def test_job_delivery_uses_gui_thread(self):
        from PySide6.QtCore import QThread
        from promptnik.ui import Job
        result=[]
        job=Job(lambda *_:42,lambda v:result.append((v,QThread.currentThread())),lambda e:self.fail(e))
        deadline=time.monotonic()+3
        while not result and time.monotonic()<deadline:
            self.app.processEvents(); time.sleep(.01)
        self.assertEqual(result[0][0],42); self.assertEqual(result[0][1],self.app.thread())

    def test_chat_sizes_font_settings_and_inline_thinking(self):
        from promptnik.storage import Store
        from promptnik.ui import Window, MessageBubble, MessageText, metrics_text
        def settle():
            for _ in range(25): self.app.processEvents()
        with tempfile.TemporaryDirectory() as root, patch('promptnik.core.discover',return_value={}), patch('promptnik.core.environment_connections',return_value={}):
            store=Store(root)
            w=Window(store)
            try:
                w.connections={'local':{'url':'http://localhost:11434/v1','model':'test'}}
                session=store.new_session('chat','local',w.connections['local'])
                session['messages']=[{'role':'user','text':'Hi'}, {'role':'assistant','text':'# Long reply\n\n'+('\n\n'.join(['A long paragraph of wrapped prose. '*8]*20)), 'thinking':'Reasoning text'}]
                w.open_session(session); w.show(); settle(); tab=w.tabs.currentWidget()
                bubbles=tab.findChildren(MessageBubble)
                short,long=bubbles
                self.assertLess(short.text.height(),50)
                self.assertGreater(long.text.height(),500)
                self.assertEqual(long.text.verticalScrollBar().maximum(),0)
                self.assertGreater(short.parentWidget().layout().contentsMargins().left(),long.parentWidget().layout().contentsMargins().left())
                self.assertFalse(long.thinking_view.isVisible()); long.thinking_toggle.click(); settle(); self.assertTrue(long.thinking_view.isVisible())
                long.thinking_toggle.click(); self.assertFalse(long.thinking_view.isVisible())
                self.assertLessEqual(tab.composer.height(),80)
                tab.chat_split.setSizes([350,180]); settle(); self.assertGreater(tab.composer.height(),100)
                # Toolbar font controls apply immediately and persist the size.
                self.assertTrue(w.menuBar().isHidden() or not w.menuBar().actions())
                w.apply_font_size(15); w.font_larger.click(); w.theme.setCurrentText('Dark'); settle()
                self.assertEqual(short.text.font().pointSize(),16)
                self.assertEqual(short.text.document().defaultFont().pointSize(),16)
                self.assertEqual(store.config['settings']['font_size'],16)
                self.assertEqual(store.config['settings']['theme'],'Dark')
                w.font_smaller.click(); settle(); self.assertEqual(short.text.font().pointSize(),15)
                self.assertEqual(store.config['settings']['font_size'],15)
                self.assertEqual(long.text.verticalScrollBar().maximum(),0)
                w.resize(1000,700); settle(); self.assertEqual(long.text.verticalScrollBar().maximum(),0)
                # A live assistant card reveals reasoning once and respects subsequent toggles.
                live=tab.add_bubble({'role':'assistant','text':'','thinking':''},live=True); settle()
                live.set_stream('','Reasoning arrives'); settle(); self.assertTrue(live.thinking_view.isVisible())
                live.thinking_toggle.click(); live.set_stream('**Answer**','More reasoning'); settle()
                self.assertFalse(live.thinking_view.isVisible()); self.assertEqual(live.text.toPlainText(),'Answer')
                live.text.set_content('```python\n'+'x'*1000+'\n```',True); settle()
                self.assertEqual(live.text.horizontalScrollBar().maximum(),0)
                self.assertEqual(live.text.verticalScrollBar().maximum(),0)
                self.assertIn('Overall 25.00 tok/s',metrics_text({'input_tokens':80,'output_tokens':20,'total_seconds':4}))
                self.assertNotIn('Overall',metrics_text({'input_tokens':80,'output_tokens':20,'total_seconds':4,'prompt_tps':100,'generation_tps':10}))
            finally:
                w.close(); self.app.processEvents(); store.db.close()

    def test_branding_assets_and_font_limits(self):
        from PySide6.QtGui import QImage, QPalette
        from promptnik.storage import Store
        from promptnik.ui import Window
        assets=Path(__file__).resolve().parents[1]/'promptnik'/'assets'
        for name in ('promptnik-light.png','promptnik-dark.png','promptnik-icon.png'):
            image=QImage(str(assets/name)); self.assertFalse(image.isNull()); self.assertTrue(image.hasAlphaChannel()); self.assertEqual(image.pixelColor(0,0).alpha(),0)
            svg=(assets/name).with_suffix('.svg').read_text('utf-8')
            self.assertIn('<path',svg); self.assertNotIn('<image',svg)
        with tempfile.TemporaryDirectory() as root, patch('promptnik.core.discover',return_value={}), patch('promptnik.core.environment_connections',return_value={}):
            store=Store(root); w=Window(store)
            try:
                w.theme.setCurrentText('Light'); self.assertEqual(w.brand_variant,'light'); self.assertFalse(w.brand.pixmap().isNull()); self.assertFalse(w.windowIcon().isNull()); self.assertFalse(w.welcome_brand.pixmap().isNull())
                self.assertGreater(self.app.palette().color(QPalette.ColorRole.Window).lightness(),128)
                w.theme.setCurrentText('Dark'); self.assertEqual(w.brand_variant,'dark')
                self.assertLess(self.app.palette().color(QPalette.ColorRole.Window).lightness(),128)
                w.apply_font_size(8); self.assertFalse(w.font_smaller.isEnabled()); w.font_larger.click(); self.assertEqual(store.config['settings']['font_size'],9)
                w.apply_font_size(32); self.assertFalse(w.font_larger.isEnabled()); w.font_smaller.click(); self.assertEqual(store.config['settings']['font_size'],31)
            finally:
                w.close(); self.app.processEvents(); store.db.close()

    def test_auto_models_refresh_and_chart_button(self):
        from PySide6.QtWidgets import QPushButton, QStackedWidget
        from promptnik.storage import Store
        from promptnik.ui import Window, DecisionResult, ConnectionDialog
        with tempfile.TemporaryDirectory() as root, patch('promptnik.core.discover',return_value={}), patch('promptnik.core.environment_connections',return_value={}), patch('promptnik.core.models',return_value=['example-model']) as models:
            store=Store(root); w=Window(store)
            try:
                self.assertEqual(ConnectionDialog(w).protocol.currentText(),'responses')
                w.connections={'test':{'url':'http://localhost:11434/v1','protocol':'chat','model':''}}
                w.new_session('chat','test'); tab=w.tabs.currentWidget(); w.show()
                deadline=time.monotonic()+3
                while tab.model.currentText()!='example-model' and time.monotonic()<deadline:
                    self.app.processEvents(); time.sleep(.01)
                self.assertEqual(tab.model.currentText(),'example-model'); self.assertEqual(models.call_count,1)
                tab.models_button.click()
                deadline=time.monotonic()+3
                while models.call_count<2 and time.monotonic()<deadline: self.app.processEvents(); time.sleep(.01)
                self.assertEqual(models.call_count,2)
                chart=DecisionResult({'answers':{'q':{'type':'choice','choice':'a','probabilities':{'a':.8,'b':.2}}}})
                toggle=chart.findChild(QPushButton); stack=chart.findChild(QStackedWidget)
                self.assertEqual(stack.currentIndex(),0); toggle.click(); self.assertEqual(stack.currentIndex(),1); toggle.click(); self.assertEqual(stack.currentIndex(),0)
            finally:
                w.close(); self.app.processEvents(); store.db.close()

    def test_decision_state_accepts_images_and_text(self):
        from PySide6.QtCore import QMimeData, QUrl
        from PySide6.QtGui import QImage, QColor
        from promptnik.storage import Store
        from promptnik.ui import Window
        with tempfile.TemporaryDirectory() as root, patch('promptnik.core.discover',return_value={}), patch('promptnik.core.environment_connections',return_value={}), patch('promptnik.core.models',return_value=[]):
            store=Store(root); w=Window(store)
            try:
                w.connections={'test':{'url':'http://localhost:11434/v1','protocol':'chat','model':'test'}}
                w.new_session('decision','test'); tab=w.tabs.currentWidget(); w.show(); self.app.processEvents()
                image=QImage(24,24,QImage.Format.Format_RGB32); image.fill(QColor('red'))
                path=Path(root)/'input.png'; self.assertTrue(image.save(str(path)))
                dropped=QMimeData(); dropped.setUrls([QUrl.fromLocalFile(str(path))]); tab.state.insertFromMimeData(dropped)
                self.assertEqual(len(tab.attachments.paths),1); self.assertEqual(tab.state.toPlainText(),'')
                pasted=QMimeData(); pasted.setImageData(image); tab.state.insertFromMimeData(pasted)
                self.assertEqual(len(tab.attachments.paths),2)
                text=QMimeData(); text.setText('{"status":"ok"}'); tab.state.insertFromMimeData(text)
                self.assertEqual(tab.state.toPlainText(),'{"status":"ok"}')
                tab.json_state.setChecked(True); tab.save_draft()
                saved=store.list_sessions()[0]['form']; self.assertTrue(saved['json_state']); self.assertEqual(len(saved['images']),2)
            finally:
                w.close(); self.app.processEvents(); store.db.close()

    def test_prompts_panel_saves_filters_and_copies_both_types(self):
        from promptnik.storage import Store
        from promptnik.ui import Window, MessageBubble
        with tempfile.TemporaryDirectory() as root, patch('promptnik.core.discover',return_value={}), patch('promptnik.core.environment_connections',return_value={}), patch('promptnik.core.models',return_value=[]), patch('promptnik.ui.confirm',return_value=True):
            store=Store(root); store.config['connections']={'first':{'url':'http://localhost:11434/v1','model':'one'},'second':{'url':'http://localhost:11434/v1','model':'two'}}; w=Window(store)
            try:
                w.new_session('chat','first'); first=w.tabs.currentWidget(); w.show(); self.app.processEvents()
                image=store.attach(data=b'image'); first.composer.setPlainText('Find the truck'); first.attachments.set_paths([image]); first.save_draft(); first.star_current.click()
                self.assertEqual(w.prompts_list.count(),1)
                first.session['messages']=[{'role':'user','text':'Earlier prompt','images':[]}]; first.render()
                historical=first.findChild(MessageBubble).findChildren(__import__('PySide6.QtWidgets',fromlist=['QPushButton']).QPushButton)
                next(b for b in historical if b.accessibleName()=='Save prompt').click()
                self.assertEqual(w.prompts_list.count(),2)
                w.prompts_search.setText('truck'); self.assertEqual(w.prompts_list.count(),1)
                w.new_session('chat','second'); second=w.tabs.currentWidget(); self.app.processEvents()
                w.prompts_list.setCurrentRow(0); w.copy_selected_prompt()
                self.assertEqual(second.composer.toPlainText(),'Find the truck'); self.assertEqual(second.attachments.paths,[image])
                w.new_session('decision','first'); decision=w.tabs.currentWidget(); self.app.processEvents()
                self.assertEqual(w.prompts_list.count(),0); w.prompts_search.clear()
                decision.state.setPlainText('Check quality'); q=decision.questions[0]; q.instructions.setPlainText('Which category?'); q.kind.setCurrentIndex(1); q.add_option(['good','Good']); q.add_option(['bad','Bad']); decision.star_current.click()
                self.assertEqual(w.prompts_list.count(),1); w.prompts_search.setText('category'); self.assertEqual(w.prompts_list.count(),1)
                w.new_session('decision','second'); other=w.tabs.currentWidget(); self.app.processEvents(); w.prompts_list.setCurrentRow(0); w.copy_selected_prompt()
                self.assertEqual(other.state.toPlainText(),'Check quality'); self.assertEqual(other.questions[0].value()['choices'],[['good','Good'],['bad','Bad']])
                w.prompts_search.clear(); w.tabs.setCurrentWidget(second); self.app.processEvents(); self.assertEqual(w.prompts_list.count(),2)
                w.delete_ids([first.session['id']]); self.assertTrue(Path(image).exists())
                w.toggle_prompts(); self.assertTrue(w.prompts_panel.isHidden()); w.toggle_prompts(); self.assertFalse(w.prompts_panel.isHidden())
            finally:
                w.close(); self.app.processEvents(); store.db.close()

if __name__=='__main__': unittest.main()
