"""Qt desktop interface. Network jobs never run on the GUI thread."""
import copy
import json
import os
from pathlib import Path
import sys
import threading
import uuid
import argparse
import datetime
import math

from PySide6.QtCore import Qt, Signal, QObject, QTimer, QByteArray, QBuffer, QIODevice, QEvent
from PySide6.QtGui import QAction, QColor, QPainter, QPixmap, QIcon, QDesktopServices, QPalette, QShortcut, QKeySequence, QTextCursor
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QCheckBox,
    QSpinBox, QDialog, QDialogButtonBox, QListWidget, QListWidgetItem, QTabWidget, QSplitter,
    QScrollArea, QFrame, QFileDialog, QMessageBox, QInputDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QProgressBar, QStackedWidget, QAbstractItemView, QSizePolicy)

from . import core
from .storage import Store

class Signals(QObject):
    progress = Signal(object)
    done = Signal(object)
    error = Signal(str)

class Job:
    def __init__(self, fn, done, error, progress=None):
        self.signals = Signals(); self.cancel = threading.Event()
        self.signals.done.connect(done); self.signals.error.connect(error)
        if progress: self.signals.progress.connect(progress)
        def work():
            try: result = fn(self.signals.progress.emit, self.cancel)
            except Exception as exc:
                try: self.signals.error.emit(str(exc))
                except RuntimeError: pass
            else:
                try: self.signals.done.emit(result)
                except RuntimeError: pass
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

def button(text, fn, layout=None):
    b = QPushButton(text); b.clicked.connect(fn)
    if layout: layout.addWidget(b)
    return b

def error(parent, message): QMessageBox.warning(parent, 'Promptnik', str(message))

def confirm(parent, text):
    return QMessageBox.question(parent, 'Promptnik', text, QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget(): item.widget().deleteLater()
        elif item.layout(): clear_layout(item.layout())

def number(value, suffix=''):
    return '—' if value is None else f'{value:,.2f}{suffix}' if isinstance(value, float) else f'{value:,}{suffix}'

def metrics_text(m):
    parts = ['Prompt '+number(m.get('input_tokens'))+' tokens', 'Generation '+number(m.get('output_tokens'))+' tokens']
    for label, key in [('Prompt', 'prompt_tps'), ('Generation', 'generation_tps')]:
        if m.get(key) is not None: parts.append(label+' '+number(m[key], ' tok/s'))
    if m.get('prompt_tps') is None or m.get('generation_tps') is None:
        total_rate = core.total_token_rate(m)
        parts.append('Overall '+number(total_rate, ' tok/s')+' (input + output / elapsed)')
    parts.extend(['Thinking '+number(m.get('thinking_seconds_estimate'), ' s (arrival estimate)'), 'Total '+number(m.get('total_seconds'), ' s')])
    return ' · '.join(parts)

class MessageText(QTextBrowser):
    """A selectable rich-text document sized to its full wrapped content."""
    def __init__(self, text='', markdown=False):
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet('QTextBrowser { background: transparent; border: none; padding: 0px; }')
        self.setOpenLinks(False); self.setOpenExternalLinks(False)
        self.anchorClicked.connect(lambda url:QDesktopServices.openUrl(url) if url.scheme() in ('https','http') else None)
        self.document().setDocumentMargin(2)
        self.document().setDefaultStyleSheet('pre { white-space: pre-wrap; }')
        self._fit_timer = QTimer(self); self._fit_timer.setSingleShot(True); self._fit_timer.timeout.connect(self.fit_content)
        self.document().documentLayout().documentSizeChanged.connect(self.schedule_fit)
        self.set_content(text,markdown)
    def set_content(self,text,markdown=False):
        if markdown:
            self.setMarkdown(text)
            # Qt imports fenced code as unbreakable preformatted lines. Allow
            # wrapping so long code stays visible without an inner scrollbar.
            block = self.document().begin()
            while block.isValid():
                format = block.blockFormat()
                if format.nonBreakableLines():
                    format.setNonBreakableLines(False); QTextCursor(block).setBlockFormat(format)
                block = block.next()
        else: self.setPlainText(text)
        self.schedule_fit()
    def schedule_fit(self,*_): self._fit_timer.start(0)
    def fit_content(self):
        width = max(1,self.viewport().width())
        if self.document().textWidth() != width: self.document().setTextWidth(width)
        margins = self.contentsMargins()
        height = math.ceil(self.document().size().height())+margins.top()+margins.bottom()+2*self.frameWidth()+2
        if self.height() != height: self.setFixedHeight(height)
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'_fit_timer'): self.schedule_fit()
    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(self,'_fit_timer'):
            self.document().setDefaultFont(self.font()); self.schedule_fit()

class MessageBubble(QFrame):
    def __init__(self,message,live=False):
        super().__init__(); self.role = message['role']; self.live = live; self._thinking_seen = False
        self._initial_thinking_visible = message.get('thinking_visible',live)
        self.setObjectName('userBubble' if self.role == 'user' else 'assistantBubble')
        self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self); layout.setContentsMargins(14,10,14,10); layout.setSpacing(6)
        heading = QLabel('You' if self.role == 'user' else 'Assistant'); heading.setStyleSheet('font-weight: bold;')
        self.header = QHBoxLayout(); self.header.addWidget(heading); self.header.addStretch(); layout.addLayout(self.header)
        self.thinking_toggle = QPushButton('Show thinking'); self.thinking_toggle.setCheckable(True)
        self.thinking_toggle.setSizePolicy(QSizePolicy.Policy.Maximum,QSizePolicy.Policy.Fixed)
        self.thinking_toggle.toggled.connect(self.toggle_thinking); layout.addWidget(self.thinking_toggle)
        self.thinking_view = MessageText(); layout.addWidget(self.thinking_view)
        self.text = MessageText(); layout.addWidget(self.text)
        for path in message.get('images',[]):
            label = QLabel(); label.setPixmap(QPixmap(path).scaled(280,180,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)); layout.addWidget(label)
        if message.get('metrics'):
            label = QLabel(metrics_text(message['metrics'])); label.setObjectName('metrics'); label.setWordWrap(True); layout.addWidget(label)
        self.set_stream(message.get('text',''),message.get('thinking',''))
        self.restyle()
    def toggle_thinking(self,visible):
        self.thinking_view.setVisible(visible); self.thinking_toggle.setText('Hide thinking' if visible else 'Show thinking')
    def set_stream(self,text,thinking):
        has_thinking = bool(thinking)
        self.thinking_toggle.setVisible(has_thinking)
        self.thinking_view.set_content(thinking)
        if has_thinking and not self._thinking_seen:
            self._thinking_seen = True; self.thinking_toggle.setChecked(self._initial_thinking_visible)
        self.thinking_view.setVisible(has_thinking and self.thinking_toggle.isChecked())
        self.text.set_content(text,self.role == 'assistant')
        self.text.setVisible(bool(text))
    def restyle(self):
        if getattr(self,'_restyling',False): return
        palette = QApplication.palette()
        base = palette.color(QPalette.ColorRole.Base)
        tint = palette.color(QPalette.ColorRole.Highlight if self.role == 'user' else QPalette.ColorRole.Text)
        weight = .15 if self.role == 'user' else .055
        color = QColor(*[round((1-weight)*a+weight*b) for a,b in zip(base.getRgb()[:3],tint.getRgb()[:3])])
        style = f'QFrame#{self.objectName()} {{ background-color: {color.name()}; border: 1px solid palette(mid); border-radius: 12px; }}'
        if self.styleSheet() != style:
            self._restyling = True
            try: self.setStyleSheet(style)
            finally: self._restyling = False
    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange,QEvent.Type.ApplicationPaletteChange) and hasattr(self,'role'): self.restyle()

class AttachmentBox(QWidget):
    changed = Signal()
    def __init__(self, store):
        super().__init__(); self.store = store; self.paths = []; self.setAcceptDrops(True)
        outer = QVBoxLayout(self); outer.setContentsMargins(0,0,0,0)
        self.list = QListWidget(); self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setIconSize(__import__('PySide6.QtCore', fromlist=['QSize']).QSize(70,55)); self.list.setMaximumHeight(106)
        self.list.itemDoubleClicked.connect(lambda item: QDesktopServices.openUrl(QUrl.fromLocalFile(item.data(Qt.ItemDataRole.UserRole))))
        outer.addWidget(self.list); row = QHBoxLayout(); outer.addLayout(row)
        button('Attach images…', self.choose, row); button('Paste image', self.paste, row); button('Remove selected', self.remove, row)
        row.addStretch(); self.refresh()
    def refresh(self):
        self.list.clear(); self.list.setVisible(bool(self.paths))
        for i, p in enumerate(self.paths):
            item = QListWidgetItem(QIcon(p), f'Image {i+1}'); item.setToolTip(p); item.setData(Qt.ItemDataRole.UserRole, p); self.list.addItem(item)
    def set_paths(self, paths): self.paths = list(paths); self.refresh()
    def add(self, paths):
        try:
            for path in paths:
                image = QPixmap(path)
                if image.isNull(): raise ValueError(f'Cannot read image: {Path(path).name}')
                core.image_block(path, 'chat')
                stored = self.store.attach(path)
                if stored not in self.paths: self.paths.append(stored)
            self.refresh(); self.changed.emit()
        except Exception as exc: error(self, exc)
    def choose(self):
        paths, _ = QFileDialog.getOpenFileNames(self, 'Attach images', '', 'Images (*.png *.jpg *.jpeg *.webp *.gif)'); self.add(paths)
    def paste(self):
        image = QApplication.clipboard().image()
        if image.isNull(): return error(self, 'The clipboard does not contain an image.')
        self.add_image(image)
    def add_image(self, image):
        if isinstance(image, QPixmap): image = image.toImage()
        if image.isNull(): return error(self, 'The supplied image could not be read.')
        buf = QBuffer(); buf.open(QIODevice.OpenModeFlag.WriteOnly); image.save(buf, 'PNG')
        self.paths.append(self.store.attach(data=bytes(buf.data()))); self.refresh(); self.changed.emit()
    def remove(self):
        selected = {x.data(Qt.ItemDataRole.UserRole) for x in self.list.selectedItems()}
        self.paths = [p for p in self.paths if p not in selected]; self.refresh(); self.changed.emit()
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls(): event.acceptProposedAction()
    def dropEvent(self, event): self.add([u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()])

class Composer(QPlainTextEdit):
    send = Signal()
    attach = Signal(object)
    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.send.emit(); return
        super().keyPressEvent(e)
    def insertFromMimeData(self, source):
        if source.hasUrls(): self.attach.emit([u.toLocalFile() for u in source.urls() if u.isLocalFile()])
        elif source.hasImage(): self.attach.emit(source.imageData())
        else: super().insertFromMimeData(source)

class Pie(QWidget):
    colors = ['#6d7dfb','#20b9a0','#f4ae55','#ec7597','#aa83ed','#54b4e5','#a7c957']
    def __init__(self, values):
        super().__init__(); self.values = values; self.setMinimumHeight(220)
    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        size = min(190, self.height()-20); x = 10; y = 10; start = 90*16
        for i, (label, value) in enumerate(self.values.items()):
            painter.setBrush(QColor(self.colors[i % len(self.colors)])); painter.setPen(Qt.PenStyle.NoPen)
            span = round(-value*360*16); painter.drawPie(x,y,size,size,start,span); start += span
            painter.setPen(self.palette().color(QPalette.ColorRole.Text))
            painter.drawText(size+30, 25+i*23, f'{label}  {value:.1%}')
        painter.end()

class DecisionResult(QWidget):
    def __init__(self, raw):
        super().__init__(); layout = QVBoxLayout(self)
        for name, answer in raw.get('answers', {}).items():
            title = answer.get('choice', answer.get('score', answer.get('noul', '')))
            heading = QLabel(f'{name} · {title}'); heading.setTextFormat(Qt.TextFormat.PlainText); layout.addWidget(heading)
            values = answer.get('probabilities', {})
            if 'noul' in answer: values = {'No':1-answer['noul'], 'Yes':answer['noul']}
            if isinstance(values, list): values = {str(i):v for i,v in enumerate(values)}
            if isinstance(values, dict) and values:
                safe = {str(k):float(v) for k,v in values.items() if isinstance(v, (int,float)) and 0 <= v <= 1}
                legend = answer.get('legend',{})
                if isinstance(legend,dict): safe = {str(legend.get(k,k)):v for k,v in safe.items()}
                stack = QStackedWidget(); bars = QWidget(); bar_layout = QVBoxLayout(bars)
                for label, probability in safe.items():
                    row = QHBoxLayout(); row.addWidget(QLabel(label)); bar = QProgressBar(); bar.setRange(0,10000); bar.setValue(round(probability*10000)); bar.setFormat(f'{probability:.2%}'); row.addWidget(bar, 1); bar_layout.addLayout(row)
                stack.addWidget(bars)
                if answer.get('type') == 'choice' and abs(sum(safe.values())-1) < .02 and len(safe) <= 26:
                    pie = Pie(safe); pie.setMinimumHeight(max(220, len(safe)*23+20)); stack.addWidget(pie)
                    toggle = QPushButton('Show pie chart'); toggle.setCheckable(True)
                    def switch(checked, stack=stack, toggle=toggle):
                        stack.setCurrentIndex(1 if checked else 0); toggle.setText('Show probability bars' if checked else 'Show pie chart')
                    toggle.toggled.connect(switch); layout.addWidget(toggle)
                layout.addWidget(stack)
            if 'confidence' in answer: layout.addWidget(QLabel(f'Confidence: {answer["confidence"]:.2%}'))

class WheelGuardCombo(QComboBox):
    def wheelEvent(self,event):
        if self.hasFocus(): super().wheelEvent(event)
        else: event.ignore()

class QuestionEditor(QFrame):
    changed = Signal()
    removed = Signal(object)
    def __init__(self, data=None):
        super().__init__(); self.setObjectName('questionCard'); outer = QVBoxLayout(self)
        row = QHBoxLayout(); outer.addLayout(row)
        self.name = QLineEdit(); self.name.setPlaceholderText('Question name, e.g. urgency'); row.addWidget(self.name)
        self.kind = WheelGuardCombo(); self.kind.setFocusPolicy(Qt.FocusPolicy.ClickFocus); self.kind.addItem('Yes / No', 'noul'); self.kind.addItem('Multiple choice', 'choice'); self.kind.addItem('Ordered score', 'score'); row.addWidget(self.kind)
        button('Remove question', lambda: self.removed.emit(self), row)
        self.instructions = QPlainTextEdit(); self.instructions.setPlaceholderText('What should the model decide?'); self.instructions.setMaximumHeight(75); outer.addWidget(self.instructions)
        self.criteria = QWidget(); criteria_layout = QVBoxLayout(self.criteria); criteria_layout.setContentsMargins(0,0,0,0)
        self.table = QTableWidget(0,2); self.table.setHorizontalHeaderLabels(['Label', 'Description / scoring level (lowest first)']); self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch); self.table.setMaximumHeight(180); criteria_layout.addWidget(self.table)
        buttons = QHBoxLayout(); criteria_layout.addLayout(buttons); button('Add option', self.add_option, buttons); button('Remove option', self.remove_option, buttons); buttons.addStretch(); outer.addWidget(self.criteria)
        self.kind.currentIndexChanged.connect(self.type_changed)
        self.name.textChanged.connect(self.changed); self.instructions.textChanged.connect(self.changed); self.table.itemChanged.connect(self.changed)
        if data:
            self.name.setText(data['name']); self.instructions.setPlainText(data['instructions']); self.kind.setCurrentIndex(max(0, self.kind.findData(data['type'])))
            for pair in data.get('choices', []): self.add_option(pair)
        self.type_changed()
    def type_changed(self):
        self.criteria.setVisible(self.kind.currentData() != 'noul'); self.table.setColumnHidden(0, self.kind.currentData() == 'score'); self.changed.emit()
    def add_option(self, pair=None):
        if not isinstance(pair, (list,tuple)): pair = ('','')
        i = self.table.rowCount(); self.table.insertRow(i)
        for j, value in enumerate(pair): self.table.setItem(i,j,QTableWidgetItem(value))
        self.changed.emit()
    def remove_option(self):
        if self.table.currentRow() >= 0: self.table.removeRow(self.table.currentRow()); self.changed.emit()
    def value(self):
        return dict(name=self.name.text().strip(), instructions=self.instructions.toPlainText(), type=self.kind.currentData(), choices=[[(self.table.item(i,j).text() if self.table.item(i,j) else '') for j in range(2)] for i in range(self.table.rowCount())])

class ConnectionDialog(QDialog):
    def __init__(self, parent, name='', data=None):
        super().__init__(parent); self.setWindowTitle('Connection'); self.resize(560,420); c = data or {}; layout = QFormLayout(self)
        self.fields = {}
        for key, label in [('name','Name'),('url','Endpoint URL'),('api_key','API key'),('model','Default model'),('reasoning','Reasoning effort (optional)')]:
            edit = QLineEdit(name if key == 'name' else c.get(key, '')); self.fields[key] = edit; layout.addRow(label, edit)
        self.fields['api_key'].setEchoMode(QLineEdit.EchoMode.Password)
        self.protocol = QComboBox(); self.protocol.addItems(core.PROTOCOLS); self.protocol.setCurrentText(c.get('protocol','responses')); layout.addRow('Chat protocol',self.protocol)
        self.context = QSpinBox(); self.context.setRange(0,100000000); self.context.setSpecialValueText('Unknown'); self.context.setValue(c.get('context_limit',0)); layout.addRow('Context limit (tokens)',self.context)
        self.stream = QCheckBox('Stream chat responses'); self.stream.setChecked(c.get('stream',True)); layout.addRow(self.stream)
        self.plain = QCheckBox('Store API key in plaintext instead of OS credential store'); layout.addRow(self.plain)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel); buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); layout.addRow(buttons)
    def value(self):
        c = {k:e.text().strip() for k,e in self.fields.items()}; name = c.pop('name')
        if not name: raise ValueError('Enter a connection name.')
        c.update(protocol=self.protocol.currentText(), context_limit=self.context.value(), stream=self.stream.isChecked())
        core.validate_connections({'connections':{name:c}})
        return name,c

class Connections(QDialog):
    changed = Signal()
    def __init__(self, window):
        super().__init__(window); self.window = window; self.setWindowTitle('Connections'); self.resize(800,520)
        layout = QVBoxLayout(self); layout.addWidget(QLabel('Saved, environment, and local connections · select an entry to edit or save a copy'))
        self.list = QListWidget(); self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); layout.addWidget(self.list)
        row = QHBoxLayout(); layout.addLayout(row)
        button('Add…', lambda:self.edit(False),row); button('Edit / Save copy…',lambda:self.edit(True),row); button('Delete saved',self.delete,row)
        self.refresh_button = button('Refresh discovery',self.scan,row); button('Import…',self.import_file,row); button('Export…',self.export_file,row)
        self.status = QLabel(''); layout.addWidget(self.status); button('Close',self.accept,layout)
        self.populate()
    def populate(self):
        selected = {x.data(Qt.ItemDataRole.UserRole) for x in self.list.selectedItems()}; self.list.clear()
        for name,c in self.window.connections.items():
            item = QListWidgetItem(f'{name}\n{c.get("url", "")}  ·  {c.get("model") or "choose a model"}'); item.setData(Qt.ItemDataRole.UserRole,name); self.list.addItem(item); item.setSelected(name in selected)
    def scan(self):
        self.refresh_button.setEnabled(False); self.status.setText('Checking loopback endpoints…')
        def done(found):
            self.window.local = found; self.window.reload_connections(); self.populate(); self.refresh_button.setEnabled(True); self.status.setText(f'{len(found)} local endpoint(s) found.')
        self.job = Job(lambda *_:core.discover(),done,lambda e:(self.refresh_button.setEnabled(True),self.status.setText(e)))
    def edit(self, selected):
        item = self.list.currentItem(); name = item.data(Qt.ItemDataRole.UserRole) if selected and item else ''
        data = self.window.connections.get(name,{})
        if name.startswith(('env:','local:')): name = name.split(':',1)[1]
        dlg = ConnectionDialog(self,name,data)
        while dlg.exec() == QDialog.DialogCode.Accepted:
            try:
                new,c = dlg.value()
                if new in self.window.store.config['connections'] and not confirm(self,f'Replace saved connection “{new}”?'): continue
                self.window.store.save_connection(new,c,dlg.plain.isChecked()); self.window.reload_connections(); self.populate(); return
            except Exception as exc: error(self,exc)
    def delete(self):
        names = [x.data(Qt.ItemDataRole.UserRole) for x in self.list.selectedItems()]
        names = [n for n in names if n in self.window.store.config['connections']]
        if names and confirm(self,f'Delete {len(names)} saved connection(s)?'):
            for n in names: self.window.store.delete_connection(n)
            self.window.reload_connections(); self.populate()
    def import_file(self):
        path,_ = QFileDialog.getOpenFileName(self,'Import Promptnik or ChaTTY connections','','JSON (*.json)')
        if not path: return
        try:
            incoming = core.validate_connections(json.loads(Path(path).read_text('utf-8-sig')))
            preview = '\n'.join(f'{n}: {c["url"]}' for n,c in incoming.items())
            if not confirm(self,'Import these connections?\n\n'+preview): return
            for name,c in incoming.items():
                if name in self.window.store.config['connections']:
                    choice,ok = QInputDialog.getItem(self,'Duplicate connection',name,['Skip','Replace','Rename'],0,False)
                    if not ok or choice == 'Skip': continue
                    if choice == 'Rename':
                        name,ok = QInputDialog.getText(self,'Rename connection','New name:')
                        if not ok: continue
                        if not name.strip() or name in self.window.store.config['connections']: raise ValueError('New name must be unique and nonempty.')
                try: self.window.store.save_connection(name,c)
                except Exception as exc:
                    if not confirm(self,f'Credential store unavailable: {exc}\nStore “{name}” with a plaintext key?'): continue
                    self.window.store.save_connection(name,c,True)
            self.window.reload_connections(); self.populate()
        except Exception as exc: error(self,exc)

    def export_file(self):
        names = [x.data(Qt.ItemDataRole.UserRole) for x in self.list.selectedItems()]
        if not names: names = list(self.window.connections)
        include = confirm(self,'Include API keys in the export?\nChoose No to export connection settings without keys.')
        path,_ = QFileDialog.getSaveFileName(self,'Export connections','connections.json','JSON (*.json)')
        if not path: return
        try:
            result = {n:{k:v for k,v in self.window.connections[n].items() if k != 'credential_id' and (include or k != 'api_key')} for n in names}
            Path(path).write_text(json.dumps({'connections':result},indent=2)+'\n',encoding='utf-8')
        except Exception as exc: error(self,exc)

class SessionTab(QWidget):
    updated = Signal()
    def __init__(self, window, session):
        super().__init__(); self.window = window; self.store = window.store; self.session = session; self.job = None; self.loading = True; self._closed = False
        self.credentials = {}; self.questions = []; self.partial = None
        layout = QVBoxLayout(self); layout.setContentsMargins(18,12,18,12)
        top = QHBoxLayout(); layout.addLayout(top)
        self.connection = QComboBox(); top.addWidget(self.connection,2)
        self.model = QComboBox(); self.model.setEditable(True); self.model.setMinimumWidth(180); top.addWidget(self.model,2)
        self.models_button = button('↻',self.load_models,top); self.models_button.setToolTip('Refresh models'); self.models_button.setAccessibleName('Refresh models')
        self.reasoning = QLineEdit(session['connection'].get('reasoning','')); self.reasoning.setPlaceholderText('Reasoning effort'); self.reasoning.setMaximumWidth(150)
        if session['mode'] == 'chat': top.addWidget(self.reasoning)
        self.stream = QCheckBox('Stream'); self.stream.setChecked(session['connection'].get('stream',True))
        if session['mode'] == 'chat': top.addWidget(self.stream)
        self.info = QLabel(''); self.info.setWordWrap(True); layout.addWidget(self.info)
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True); self.results_widget = QWidget(); self.results = QVBoxLayout(self.results_widget); self.results.addStretch(); self.scroll.setWidget(self.results_widget)
        self.results.setSpacing(10)
        self._follow_stream = True
        self.results_widget.installEventFilter(self)
        self.scroll.verticalScrollBar().valueChanged.connect(self.track_scroll)
        if session['mode'] == 'chat':
            self.chat_split = QSplitter(Qt.Orientation.Vertical); self.chat_split.setChildrenCollapsible(False); self.chat_split.setHandleWidth(7); layout.addWidget(self.chat_split,1)
            self.chat_split.addWidget(self.scroll)
            self.composer = Composer(); self.composer.setPlaceholderText('Message · Ctrl+Enter to send · paste or drop images'); self.composer.setMinimumHeight(48); self.composer.setPlainText(session.get('draft','')); self.chat_split.addWidget(self.composer)
            self.chat_split.setStretchFactor(0,1); self.chat_split.setStretchFactor(1,0)
            self.chat_split.setSizes([600,self.store.config['settings'].get('composer_height',64)])
            self.chat_split.splitterMoved.connect(self.save_composer_height)
            self.attachments = AttachmentBox(self.store); self.attachments.set_paths(session.get('draft_images',[])); layout.addWidget(self.attachments)
            self.composer.send.connect(self.submit); self.composer.attach.connect(self.attach_mime)
            self.composer.textChanged.connect(self.save_draft)
        else:
            split = QSplitter(Qt.Orientation.Horizontal); layout.addWidget(split,1)
            form_scroll = QScrollArea(); form_scroll.setWidgetResizable(True); form_widget = QWidget(); form_layout = QVBoxLayout(form_widget); form_scroll.setWidget(form_widget); split.addWidget(form_scroll); split.addWidget(self.scroll); split.setSizes([520,460])
            form_layout.addWidget(QLabel('<b>State / context</b>'))
            self.state = Composer(); self.state.setPlaceholderText('Describe the situation, paste JSON, or drop images…'); self.state.setMinimumHeight(100); self.state.setMaximumHeight(200); self.state.setPlainText(session['form'].get('state','')); form_layout.addWidget(self.state)
            self.json_state = QCheckBox('Interpret state as JSON'); self.json_state.setChecked(session['form'].get('json_state',False)); form_layout.addWidget(self.json_state)
            self.attachments = AttachmentBox(self.store); self.attachments.set_paths(session['form'].get('images',[])); form_layout.addWidget(self.attachments)
            self.state.attach.connect(self.attach_mime)
            form_layout.addWidget(QLabel('<b>Questions</b> · evaluated against the same state'))
            self.question_layout = QVBoxLayout(); form_layout.addLayout(self.question_layout)
            for q in session['form'].get('questions',[]): self.add_question(q)
            if not self.questions: self.add_question({'name':'question_1','instructions':'','type':'noul','choices':[]})
            button('＋ Add question',lambda:self.add_question(),form_layout); form_layout.addStretch()
            self.state.textChanged.connect(self.save_draft); self.json_state.toggled.connect(self.save_draft)
        self.attachments.changed.connect(self.save_draft)
        bottom = QHBoxLayout(); layout.addLayout(bottom)
        self.star_current = button('☆ Save prompt',self.star_draft,bottom); self.star_current.setToolTip('Save the current prompt to Prompts')
        self.send_button = button('Run decision' if session['mode'] == 'decision' else 'Send',self.submit,bottom); self.send_button.setObjectName('primary')
        self.cancel_button = button('Cancel',self.cancel,bottom); self.cancel_button.setEnabled(False); bottom.addStretch()
        button('Export session…',self.export_session,bottom)
        self.refresh_connections(); self.model.setEditText(session['connection'].get('model',''))
        self.connection.currentIndexChanged.connect(self.change_connection)
        self.model.currentTextChanged.connect(self.save_draft); self.reasoning.textChanged.connect(self.save_draft); self.stream.toggled.connect(self.save_draft)
        self.loading = False; self.render(); self.update_current_star(); QTimer.singleShot(0,self.load_models)
    def add_question(self,data=None):
        q = QuestionEditor(data if isinstance(data,dict) else {'name':f'question_{len(self.questions)+1}','instructions':'','type':'noul','choices':[]})
        self.questions.append(q); self.question_layout.addWidget(q); q.changed.connect(self.save_draft); q.removed.connect(self.remove_question); self.save_draft()
    def attach_mime(self, data):
        if isinstance(data, list): self.attachments.add(data)
        else: self.attachments.add_image(data)
    def remove_question(self,q):
        self.questions.remove(q); self.question_layout.removeWidget(q); q.deleteLater(); self.save_draft()
    def refresh_connections(self):
        self.connection.blockSignals(True); self.connection.clear()
        names = list(self.window.connections)
        name = self.session['connection_name']
        if name not in names: names.insert(0,name)
        for n in names: self.connection.addItem(n,n)
        self.connection.setCurrentIndex(self.connection.findData(name)); self.connection.blockSignals(False)
    def change_connection(self):
        name = self.connection.currentData()
        if name == self.session['connection_name']: return
        self.connection.blockSignals(True); self.connection.setCurrentIndex(self.connection.findData(self.session['connection_name'])); self.connection.blockSignals(False)
        self.window.new_session(self.session['mode'],name)
    def active_connection(self):
        c = dict(self.session['connection']); name = self.session['connection_name']
        saved = self.window.connections.get(name,{})
        # Do not silently attach a key from a renamed/repointed saved connection.
        try: same = core.base_url(saved.get('url','')) == core.base_url(c['url'])
        except ValueError: same = False
        c['api_key'] = self.credentials.get('api_key', saved.get('api_key','') if same else '')
        c.update(model=self.model.currentText().strip(),reasoning=self.reasoning.text().strip(),stream=self.stream.isChecked())
        return c
    def load_models(self):
        if self._closed: return
        c = self.active_connection(); self.models_button.setEnabled(False); self.info.setText('Loading models…')
        def done(names):
            from shiboken6 import isValid
            if not isValid(self) or self._closed: return
            current = self.model.currentText(); self.model.blockSignals(True); self.model.clear(); self.model.addItems(names); self.model.setEditText(current or (names[0] if names else '')); self.model.blockSignals(False); self.save_draft(); self.models_button.setEnabled(True); self.info.setText(f'{len(names)} model(s) available.')
        def failed(e):
            from shiboken6 import isValid
            if isValid(self) and not self._closed: self.models_button.setEnabled(True); self.info.setText(e)
        self.model_job = Job(lambda *_:core.models(c),done,failed)
    def form_value(self):
        return dict(state=self.state.toPlainText(),json_state=self.json_state.isChecked(),images=list(self.attachments.paths),questions=[q.value() for q in self.questions])
    def current_prompt(self):
        if self.session['mode'] == 'decision': return self.form_value()
        return {'text':self.composer.toPlainText(), 'images':list(self.attachments.paths)}
    def star_draft(self):
        payload = self.current_prompt()
        if self.session['mode'] == 'chat' and not payload['text'].strip() and not payload['images']:
            return error(self,'Enter a prompt or attach an image first.')
        if self.session['mode'] == 'decision' and not payload['state'].strip() and not payload['images'] and not any(q['instructions'].strip() for q in payload['questions']):
            return error(self,'Enter decision context, a question, or attach an image first.')
        self.window.toggle_prompt(self.session['mode'],payload)
    def copy_prompt(self,payload):
        if self.job: return error(self,'Wait for the current request to finish before replacing the prompt.')
        current = self.current_prompt()
        if self.session['mode'] == 'chat':
            occupied = bool(current['text'].strip() or current['images'])
        else:
            occupied = bool(current['state'].strip() or current['images'] or any(q['instructions'].strip() for q in current['questions']))
        if occupied and self.store.prompt_json(self.session['mode'],current) != self.store.prompt_json(self.session['mode'],payload):
            if not confirm(self,'Replace the current prompt and attachments?'): return
        if self.session['mode'] == 'chat':
            self.loading = True; self.composer.setPlainText(payload['text']); self.attachments.set_paths(payload.get('images',[])); self.loading = False; self.save_draft()
            self.composer.setFocus()
        else: self.restore(copy.deepcopy(payload)); self.state.setFocus()
    def save_draft(self,*_):
        if self.loading: return
        self.session['connection'].update(model=self.model.currentText(),reasoning=self.reasoning.text(),stream=self.stream.isChecked())
        if self.session['mode'] == 'chat': self.session.update(draft=self.composer.toPlainText(),draft_images=list(self.attachments.paths))
        else: self.session['form'] = self.form_value()
        self.store.save(self.session); self.update_current_star(); self.updated.emit()
    def update_current_star(self):
        if not hasattr(self,'star_current'): return
        saved = bool(self.store.prompt_id(self.session['mode'],self.current_prompt()))
        self.star_current.setText('★ Saved prompt' if saved else '☆ Save prompt')
        self.star_current.setToolTip('Remove from Prompts' if saved else 'Save the current prompt to Prompts')
    def busy(self, value):
        self.send_button.setEnabled(not value); self.cancel_button.setEnabled(value)
        for widget in (self.model,self.reasoning,self.stream,self.models_button,self.attachments): widget.setEnabled(not value)
        if self.session['mode'] == 'chat': self.composer.setReadOnly(value)
    def submit(self):
        if self.job: return
        try:
            c = self.active_connection()
            if not c.get('model'): raise ValueError('Choose or enter a model first.')
            core.endpoint(c,self.session['mode'])
            # Ask for a missing session key on authentication failure; local servers need none.
            self.save_draft(); mode = self.session['mode']
            if mode == 'chat':
                text = self.composer.toPlainText().strip()
                if not text and not self.attachments.paths: return
                user = dict(role='user',text=text,images=list(self.attachments.paths),status='ok')
                body = core.chat_body(c,self.session['messages']+[user])
                self.pending = user
            else:
                form = copy.deepcopy(self.form_value()); body = core.decision_body(c,form); self.pending = form
            self.partial = {'text':'','thinking':''}; self.busy(True); self.info.setText('Running…'); self.render()
            token = uuid.uuid4().hex; self.request_token = token
            self.job = Job(lambda emit,cancel:core.run_request(c,mode,body,emit,cancel),
                           lambda value:self.finished(value) if self.request_token == token else None,
                           lambda message:self.failed(message) if self.request_token == token else None,
                           lambda value:self.progress(value) if self.request_token == token else None)
        except Exception as exc: error(self,exc)
    def progress(self,value):
        if self.job and not self.job.cancel.is_set():
            self.partial = value
            if hasattr(self,'live_bubble'): self.live_bubble.set_stream(value['text'],value['thinking'])
            self.follow_stream()
    def finished(self,result):
        if self.job and self.job.cancel.is_set(): return self.failed('Request cancelled')
        self.job = None; self.busy(False); self.partial = None
        if self.session['mode'] == 'chat':
            thinking_visible = self.live_bubble.thinking_toggle.isChecked()
            self.session['messages'].extend([self.pending,dict(role='assistant',text=result['text'],thinking=result['thinking'],thinking_visible=thinking_visible,metrics=result['metrics'],status='ok')])
            if self.session['title'] == 'New chat': self.session['title'] = (self.pending['text'] or 'Image conversation')[:60]
            self.composer.clear(); self.attachments.set_paths([])
        else:
            self.session['runs'].append(dict(form=self.pending,result=result))
            if self.session['title'] == 'New decision': self.session['title'] = str(self.pending['state'])[:60]
        self.info.setText('Completed'); self.save_draft(); self.render(); self.updated.emit()
    def failed(self,message):
        cancelled = self.job and self.job.cancel.is_set(); self.job = None; self.busy(False); self.partial = None
        self.info.setText('Cancelled. The server may still finish processing.' if cancelled else message)
        if not cancelled and ('401' in message or '403' in message):
            key,ok = QInputDialog.getText(self,'Endpoint authentication','API key for this tab (not saved):',QLineEdit.EchoMode.Password)
            if ok: self.credentials['api_key'] = key
        self.render()
    def cancel(self):
        if self.job:
            self.job.cancel.set(); self.request_token = None; self.failed('Request cancelled')
    def save_composer_height(self,*_):
        if not self.loading:
            self.store.config['settings']['composer_height'] = self.chat_split.sizes()[1]; self.store.flush()
    def track_scroll(self,value):
        self._follow_stream = self.scroll.verticalScrollBar().maximum()-value < 32
    def follow_stream(self):
        if self._follow_stream:
            self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum())
    def eventFilter(self,watched,event):
        if watched is self.results_widget and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0,self.follow_stream)
        return super().eventFilter(watched,event)
    def add_bubble(self,message,live=False):
        row = QWidget(); line = QHBoxLayout(row)
        line.setContentsMargins(64 if message['role']=='user' else 4,0,4 if message['role']=='user' else 64,0)
        bubble = MessageBubble(message,live); line.addWidget(bubble); self.results.addWidget(row)
        if message['role'] == 'user':
            edit = QPushButton('✎'); edit.setAccessibleName('Edit and resend prompt'); edit.setToolTip('Edit this prompt and its images in the composer, then send again')
            bubble.header.addWidget(edit)
            edit.clicked.connect(lambda checked=False,m=message:self.copy_prompt(m))
            saved = bool(self.store.prompt_id('chat',message))
            star = QPushButton('★' if saved else '☆'); star.setAccessibleName('Remove saved prompt' if saved else 'Save prompt'); star.setToolTip('Remove from Prompts' if saved else 'Save to Prompts')
            bubble.header.addWidget(star)
            star.clicked.connect(lambda checked=False,m=message:self.window.toggle_prompt('chat',m)); self.prompt_stars.append((star,'chat',message,True))
        if not live and message['role'] == 'assistant':
            def remember_thinking(visible):
                message['thinking_visible'] = visible; self.store.save(self.session)
            bubble.thinking_toggle.toggled.connect(remember_thinking)
        return bubble
    def render(self):
        clear_layout(self.results)
        self.prompt_stars = []
        if self.session['mode'] == 'chat':
            for m in self.session['messages']:
                self.add_bubble(m)
        else:
            for i,run in enumerate(self.session['runs']):
                self.results.addWidget(QLabel(f'<b>Run {i+1}</b>'))
                saved = bool(self.store.prompt_id('decision',run['form']))
                star = button('★ Saved prompt' if saved else '☆ Save prompt',lambda checked=False,r=run:self.window.toggle_prompt('decision',r['form']),self.results)
                self.prompt_stars.append((star,'decision',run['form'],False))
                button('Restore query to form',lambda checked=False,r=run:self.restore(r['form']),self.results)
                self.results.addWidget(DecisionResult(run['result']['raw']))
                label = QLabel(metrics_text(run['result']['metrics'])); label.setObjectName('metrics'); label.setWordWrap(True); self.results.addWidget(label)
                button('Inspect query and response',lambda checked=False,r=run:self.show_text('Decision run',json.dumps(r,indent=2)),self.results)
        if self.partial is not None:
            if self.session['mode'] == 'chat':
                self.add_bubble(self.pending)
                self.live_bubble = self.add_bubble(dict(role='assistant',**self.partial),live=True)
            else: self.results.addWidget(QLabel('Running…'))
        elif not self.session['messages'] and not self.session['runs']:
            hint = QLabel('Build a decision query on the left.' if self.session['mode']=='decision' else 'Start a conversation. Attach images or paste them into the composer.'); hint.setWordWrap(True); self.results.addWidget(hint)
        self.results.addStretch(); self._follow_stream = True; QTimer.singleShot(0,self.follow_stream)
    def refresh_prompt_stars(self):
        self.update_current_star()
        for star,mode,payload,compact in self.prompt_stars:
            saved = bool(self.store.prompt_id(mode,payload))
            star.setText(('★' if saved else '☆') if compact else ('★ Saved prompt' if saved else '☆ Save prompt'))
            star.setToolTip('Remove from Prompts' if saved else 'Save to Prompts')
            star.setAccessibleName('Remove saved prompt' if saved else 'Save prompt')
    def restore(self,form):
        self.loading = True; self.state.setPlainText(form['state']); self.json_state.setChecked(form.get('json_state',False)); self.attachments.set_paths(form.get('images',[])); clear_layout(self.question_layout); self.questions = []
        for q in form['questions']: self.add_question(q)
        self.loading = False; self.save_draft()
    def show_text(self,title,text):
        dlg = QDialog(self); dlg.setWindowTitle(title); dlg.resize(720,520); layout = QVBoxLayout(dlg); view = QPlainTextEdit(); view.setReadOnly(True); view.setPlainText(text); layout.addWidget(view); button('Close',dlg.accept,layout); dlg.exec()
    def export_session(self):
        path,_ = QFileDialog.getSaveFileName(self,'Export session','conversation.json','JSON (*.json)')
        if path:
            try:
                self.save_draft(); Path(path).write_text(json.dumps(self.session,indent=2)+'\n',encoding='utf-8')
            except Exception as exc: error(self,exc)

class Window(QMainWindow):
    def __init__(self,store):
        super().__init__(); self.store = store; self.local = {}; self.connections = {}; self.setWindowTitle('Promptnik'); self.resize(1320,850)
        self._system_palette = QPalette(QApplication.palette())
        toolbar = self.addToolBar('Promptnik'); toolbar.setMovable(False)
        self.brand = QLabel(); self.brand.setContentsMargins(2,0,12,0); toolbar.addWidget(self.brand)
        for text, fn in [('☰ History',self.toggle_history),('☆ Prompts',self.toggle_prompts),('＋ Chat',lambda:self.new_session('chat')),('＋ Decision',lambda:self.new_session('decision')),('Connections',self.manage_connections)]:
            action = QAction(text,self); action.triggered.connect(fn); toolbar.addAction(action)
        toolbar.addSeparator(); toolbar.addWidget(QLabel('Theme: ')); self.theme = QComboBox(); self.theme.addItems(['System','Light','Dark']); toolbar.addWidget(self.theme); self.theme.setCurrentText(store.config['settings'].get('theme','System')); self.theme.currentTextChanged.connect(self.apply_theme)
        self.font_smaller = QPushButton('A−'); self.font_smaller.setAccessibleName('Decrease font size'); self.font_smaller.setToolTip('Decrease font size'); self.font_smaller.clicked.connect(lambda:self.adjust_font_size(-1)); toolbar.addWidget(self.font_smaller)
        self.font_larger = QPushButton('A+'); self.font_larger.setAccessibleName('Increase font size'); self.font_larger.setToolTip('Increase font size'); self.font_larger.clicked.connect(lambda:self.adjust_font_size(1)); toolbar.addWidget(self.font_larger)
        self.split = QSplitter(); self.setCentralWidget(self.split)
        self.sidebar = QWidget(); side = QVBoxLayout(self.sidebar); side.addWidget(QLabel('<b>History</b>'))
        self.search = QLineEdit(); self.search.setPlaceholderText('Search conversations'); self.search.textChanged.connect(self.refresh_history); side.addWidget(self.search)
        self.history = QListWidget(); self.history.itemActivated.connect(self.open_item); self.history.itemClicked.connect(self.open_item); side.addWidget(self.history,1)
        row = QHBoxLayout(); side.addLayout(row); button('Rename',self.rename,row); button('Delete',self.delete,row); button('Clear all',self.clear_all,side)
        self.split.addWidget(self.sidebar); self.tabs = QTabWidget(); self.tabs.setTabsClosable(True); self.tabs.setMovable(True); self.tabs.tabCloseRequested.connect(self.close_tab); self.tabs.currentChanged.connect(self.stats); self.split.addWidget(self.tabs)
        self.prompts_panel = QWidget(); prompts = QVBoxLayout(self.prompts_panel); prompts.addWidget(QLabel('<b>Prompts</b>'))
        self.prompts_search = QLineEdit(); self.prompts_search.setPlaceholderText('Search prompts'); self.prompts_search.textChanged.connect(self.refresh_prompts); prompts.addWidget(self.prompts_search)
        self.prompts_list = QListWidget(); self.prompts_list.setTextElideMode(Qt.TextElideMode.ElideRight); self.prompts_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff); self.prompts_list.itemDoubleClicked.connect(lambda _:self.copy_selected_prompt()); prompts.addWidget(self.prompts_list,1)
        self.prompt_preview = QPlainTextEdit(); self.prompt_preview.setReadOnly(True); self.prompt_preview.setAccessibleName('Saved prompt details'); self.prompt_preview.setPlaceholderText('Select a prompt to see its full contents'); self.prompt_preview.setMaximumHeight(260); prompts.addWidget(self.prompt_preview)
        self.prompts_list.currentItemChanged.connect(self.preview_selected_prompt)
        prompt_buttons = QHBoxLayout(); prompts.addLayout(prompt_buttons)
        button('Copy to tab',self.copy_selected_prompt,prompt_buttons); button('Remove',self.remove_selected_prompt,prompt_buttons)
        self.split.addWidget(self.prompts_panel); self.split.setSizes([250,820,250])
        self.prompts_panel.setVisible(store.config['settings'].get('prompts_visible',True))
        self.sidebar.setVisible(store.config['settings'].get('history_visible',True)); self.reload_connections(); self.refresh_history(); self.apply_theme()
        self.apply_font_size(store.config['settings'].get('font_size',max(8,self.font().pointSize())))
        self.statusBar().showMessage('Discovering local endpoints…')
        self.discovery = Job(lambda *_:core.discover(),self.discovered,lambda e:self.statusBar().showMessage(e))
        QShortcut(QKeySequence('Ctrl+N'),self,activated=lambda:self.new_session('chat'))
        QShortcut(QKeySequence('Ctrl+B'),self,activated=self.toggle_history)
        self.empty = QWidget(); welcome = QVBoxLayout(self.empty); welcome.addStretch()
        self.welcome_brand = QLabel(); self.welcome_brand.setAlignment(Qt.AlignmentFlag.AlignCenter); welcome.addWidget(self.welcome_brand)
        hint = QLabel('Open a chat or decision tab to begin.\nConnections are discovered from your environment and local servers.'); hint.setAlignment(Qt.AlignmentFlag.AlignCenter); hint.setWordWrap(True); welcome.addWidget(hint); welcome.addStretch()
        self.tabs.addTab(self.empty,'Welcome'); self.update_branding()
        QApplication.styleHints().colorSchemeChanged.connect(self.system_theme_changed)
    def apply_theme(self,*_):
        value = self.theme.currentText(); self.store.config['settings']['theme'] = value; self.store.flush()
        scheme = {'Light':Qt.ColorScheme.Light,'Dark':Qt.ColorScheme.Dark}.get(value,Qt.ColorScheme.Unknown)
        QApplication.styleHints().setColorScheme(scheme)
        dark = value == 'Dark' or (value == 'System' and QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark)
        window,base,text,edge = ('#20232b','#171a21','#eef0f5','#4a5261') if dark else ('#fafafa','#ffffff','#18202d','#c3c8d0')
        palette = QPalette(self._system_palette)
        for role,color in [(QPalette.ColorRole.Window,window),(QPalette.ColorRole.Base,base),(QPalette.ColorRole.AlternateBase,window),(QPalette.ColorRole.WindowText,text),(QPalette.ColorRole.Text,text),(QPalette.ColorRole.Button,window),(QPalette.ColorRole.ButtonText,text),(QPalette.ColorRole.Highlight,'#5969df'),(QPalette.ColorRole.HighlightedText,'#ffffff')]: palette.setColor(role,QColor(color))
        QApplication.setPalette(palette)
        size = self.store.config['settings'].get('font_size',max(8,self.font().pointSize()))
        colors = f'QWidget {{ font-size: {size}pt; background-color: {window}; color: {text}; }} QLabel {{ background: transparent; }} QLineEdit,QPlainTextEdit,QTextBrowser,QListWidget,QTableWidget {{ background-color: {base}; border: 1px solid {edge}; selection-background-color: #5969df; }} QPushButton,QComboBox {{ border: 1px solid {edge}; border-radius: 4px; }} QPushButton:disabled {{ color: #808795; }} QTabWidget::pane {{ border: 1px solid {edge}; }} QTabBar::tab:selected {{ background-color: {base}; }}'
        muted = '#aab3c2' if dark else '#687382'
        self.setStyleSheet(colors+f' QLabel#metrics {{ font-size: {max(7,size*.8):g}pt; color: {muted}; }}'+'QToolBar { spacing: 9px; padding: 8px; } QPushButton { padding: 6px 11px; } QLineEdit, QComboBox { padding: 5px; } QTabBar::tab { padding: 9px 16px; } QPushButton#primary { background: #5969df; color: white; border-radius: 5px; padding: 9px 24px; } QFrame#questionCard { border: 1px solid #808080; border-radius: 5px; } QProgressBar { min-height: 19px; text-align: center; } QProgressBar::chunk { background: #6d7dfb; }')
        self.update_branding()
    def apply_font_size(self,size):
        size = max(8,min(32,size))
        font = QApplication.font(); font.setPointSize(size); QApplication.setFont(font)
        self.store.config['settings']['font_size'] = size; self.apply_theme()
        self.font_smaller.setEnabled(size>8); self.font_larger.setEnabled(size<32)
        self.font_smaller.setToolTip(f'Decrease font size (currently {size} pt)'); self.font_larger.setToolTip(f'Increase font size (currently {size} pt)')
    def adjust_font_size(self,delta):
        self.apply_font_size(self.store.config['settings'].get('font_size',self.font().pointSize())+delta)
    def system_theme_changed(self,*_):
        if self.theme.currentText() == 'System': self.apply_theme()
        else: self.update_branding()
    def update_branding(self,*_):
        dark = self.theme.currentText() == 'Dark' or (self.theme.currentText() == 'System' and QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark)
        assets = Path(__file__).parent/'assets'
        self.brand_variant = 'dark' if dark else 'light'
        path = assets/f'promptnik-{self.brand_variant}.svg'
        logo = QPixmap(str(path if path.exists() else path.with_suffix('.png')))
        if not logo.isNull():
            ratio = self.devicePixelRatioF()
            header = logo.scaledToHeight(round(34*ratio),Qt.TransformationMode.SmoothTransformation); header.setDevicePixelRatio(ratio)
            self.brand.setPixmap(header); self.brand.setToolTip('Promptnik')
            if hasattr(self,'welcome_brand'):
                welcome = logo.scaledToWidth(round(540*ratio),Qt.TransformationMode.SmoothTransformation); welcome.setDevicePixelRatio(ratio); self.welcome_brand.setPixmap(welcome)
        else: self.brand.setText('Promptnik')
        icon = QIcon(str(assets/'promptnik-icon.svg'))
        self.setWindowIcon(icon); QApplication.setWindowIcon(icon)
    def reload_connections(self):
        self.connections = {**core.merge_discovered_connections(core.environment_connections(),self.local),**self.store.connections()}
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            if isinstance(widget,SessionTab): widget.refresh_connections()
    def discovered(self,found): self.local = found; self.reload_connections(); self.stats()
    def manage_connections(self): Connections(self).exec()
    def toggle_history(self):
        self.sidebar.setVisible(not self.sidebar.isVisible()); self.store.config['settings']['history_visible'] = not self.sidebar.isHidden(); self.store.flush()
    def toggle_prompts(self):
        self.prompts_panel.setVisible(self.prompts_panel.isHidden()); self.store.config['settings']['prompts_visible'] = not self.prompts_panel.isHidden(); self.store.flush()
    def refresh_prompts(self,*_):
        if not hasattr(self,'prompts_list'): return
        current = self.prompts_list.currentItem(); selected = current.data(Qt.ItemDataRole.UserRole) if current else None
        self.prompts_list.clear(); tab = self.tabs.currentWidget()
        if not isinstance(tab,SessionTab): return
        needle = self.prompts_search.text().casefold()
        for prompt in self.store.list_prompts(tab.session['mode']):
            payload = prompt['payload']; text = payload.get('text',payload.get('state',''))
            questions = ' '.join(q.get('name','')+' '+q.get('instructions','')+' '+' '.join(' '.join(choice) for choice in q.get('choices',[])) for q in payload.get('questions',[]))
            if needle not in (text+' '+questions).casefold(): continue
            title = ' '.join(text.split()) or ' '.join(questions.split()) or 'Image prompt'
            count = len(payload.get('images',[])); question_count = len(payload.get('questions',[]))
            item = QListWidgetItem(title[:100]+(f'  ·  {question_count} question(s)' if prompt['mode']=='decision' else '')+(f'  ·  {count} image(s)' if count else ''))
            item.setToolTip(text or questions); item.setData(Qt.ItemDataRole.UserRole,prompt['id']); self.prompts_list.addItem(item)
            if prompt['id'] == selected: self.prompts_list.setCurrentItem(item)
    def toggle_prompt(self,mode,payload):
        ident = self.store.prompt_id(mode,payload)
        if ident: self.store.delete_prompt(ident)
        else: self.store.save_prompt(mode,payload)
        self.prompts_changed()
    def prompts_changed(self):
        self.refresh_prompts()
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab,SessionTab): tab.refresh_prompt_stars()
    def selected_prompt(self):
        item = self.prompts_list.currentItem(); tab = self.tabs.currentWidget()
        if not item or not isinstance(tab,SessionTab): return None
        return next((p for p in self.store.list_prompts(tab.session['mode']) if p['id'] == item.data(Qt.ItemDataRole.UserRole)),None)
    def copy_selected_prompt(self):
        prompt = self.selected_prompt()
        if prompt: self.tabs.currentWidget().copy_prompt(prompt['payload'])
    def preview_selected_prompt(self,*_):
        prompt = self.selected_prompt()
        if not prompt:
            self.prompt_preview.clear(); return
        payload = prompt['payload']
        if prompt['mode'] == 'chat': lines = [payload.get('text','')]
        else:
            lines = ['State / context:',payload.get('state',''),'','Interpret state as JSON: '+('Yes' if payload.get('json_state') else 'No')]
            for i,q in enumerate(payload.get('questions',[]),1):
                kind = q.get('type','noul')
                lines.extend(['',f"Question {i}: {q.get('name','')}",{'noul':'Yes / No','choice':'Multiple choice','score':'Ordered score'}.get(kind,kind),q.get('instructions','')])
                if kind == 'noul': lines.append('Answers: Yes / No')
                elif kind == 'score': lines.extend(['Scoring levels (lowest first):',*[f'{j}. {pair[1]}' for j,pair in enumerate(q.get('choices',[]),1)]])
                else: lines.extend(['Answer choices:',*[f'{label}: {description}' for label,description in q.get('choices',[])]])
        if payload.get('images'): lines.extend(['','Images:',*payload['images']])
        self.prompt_preview.setPlainText('\n'.join(lines))
    def remove_selected_prompt(self):
        prompt = self.selected_prompt()
        if prompt and confirm(self,'Remove this saved prompt?'):
            self.store.delete_prompt(prompt['id']); self.prompts_changed()
    def new_session(self,mode,name=None):
        if not self.connections:
            self.manage_connections()
            if not self.connections: return
        if name is None:
            name,ok = QInputDialog.getItem(self,'New decision' if mode=='decision' else 'New chat','Connection:',list(self.connections),0,False)
            if not ok: return
        session = self.store.new_session(mode,name,self.connections[name]); self.store.save(session); self.open_session(session); self.refresh_history()
    def open_session(self,session):
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab,SessionTab) and tab.session['id'] == session['id']: self.tabs.setCurrentIndex(i); return
        tab = SessionTab(self,session); tab.updated.connect(self.session_updated)
        if hasattr(self,'empty') and self.tabs.indexOf(self.empty) >= 0: self.tabs.removeTab(self.tabs.indexOf(self.empty))
        self.tabs.setCurrentIndex(self.tabs.addTab(tab,session['title'])); self.stats()
    def open_item(self,item):
        session = next((s for s in self.store.list_sessions() if s['id']==item.data(Qt.ItemDataRole.UserRole)),None)
        if session: self.open_session(session)
    def refresh_history(self,*_):
        selected = self.history.currentItem().data(Qt.ItemDataRole.UserRole) if self.history.currentItem() else None
        self.history.clear(); needle = self.search.text().lower()
        for s in self.store.list_sessions():
            if needle not in (s['title']+' '+s['connection_name']).lower(): continue
            stamp = datetime.datetime.fromtimestamp(s['updated']).strftime('%b %d · %H:%M')
            item = QListWidgetItem(('◇ ' if s['mode']=='decision' else '○ ')+s['title']+'\n'+s['connection_name']+'\n'+stamp); item.setData(Qt.ItemDataRole.UserRole,s['id']); self.history.addItem(item)
            if s['id']==selected: self.history.setCurrentItem(item)
    def session_updated(self):
        self.refresh_history()
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab,SessionTab): self.tabs.setTabText(i,tab.session['title'])
        self.stats()
    def close_tab(self,index):
        tab = self.tabs.widget(index)
        if isinstance(tab,SessionTab):
            if tab.job: return error(self,'Cancel the running request and wait for it to finish before closing this tab.')
            tab.save_draft()
            tab._closed = True
        self.tabs.removeTab(index); tab.deleteLater(); self.stats()
    def rename(self):
        item = self.history.currentItem()
        if not item: return
        session = next(s for s in self.store.list_sessions() if s['id']==item.data(Qt.ItemDataRole.UserRole))
        name,ok = QInputDialog.getText(self,'Rename session','Title:',text=session['title'])
        if ok and name.strip():
            session['title'] = name.strip()
            for i in range(self.tabs.count()):
                tab = self.tabs.widget(i)
                if isinstance(tab,SessionTab) and tab.session['id']==session['id']: tab.session['title']=name.strip()
            self.store.save(session); self.session_updated()
    def delete_ids(self,ids):
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab,SessionTab) and tab.session['id'] in ids and tab.job:
                return error(self,'Cancel running requests before deleting their sessions.')
        for i in reversed(range(self.tabs.count())):
            tab = self.tabs.widget(i)
            if isinstance(tab,SessionTab) and tab.session['id'] in ids: tab._closed = True; self.tabs.removeTab(i); tab.deleteLater()
        self.store.delete(ids); self.refresh_history(); self.stats()
    def delete(self):
        item = self.history.currentItem()
        if item and confirm(self,'Delete this session and its unshared stored images?'): self.delete_ids([item.data(Qt.ItemDataRole.UserRole)])
    def clear_all(self):
        if confirm(self,'Delete ALL chat and decision history, including drafts and stored images? Saved connections will remain.'):
            self.delete_ids([s['id'] for s in self.store.list_sessions()])
    def stats(self,*_):
        self.refresh_prompts()
        tab = self.tabs.currentWidget()
        if not isinstance(tab,SessionTab): self.statusBar().showMessage('Ready · New chat: Ctrl+N · Toggle history: Ctrl+B'); return
        s = tab.session; measurements = [m['metrics'] for m in s['messages'] if 'metrics' in m] if s['mode']=='chat' else [r['result']['metrics'] for r in s['runs']]
        total_in = sum(m.get('input_tokens',0) for m in measurements); total_out = sum(m.get('output_tokens',0) for m in measurements)
        unknown = any('input_tokens' not in m or 'output_tokens' not in m for m in measurements)
        last = measurements[-1] if measurements else {}; context = last.get('input_tokens'); limit = last.get('context_limit') or s['connection'].get('context_limit')
        text = f'Context (last input): {number(context)} / {number(limit or None)}'
        if context is not None and limit: text += f' ({context/limit:.1%})'
        self.statusBar().showMessage(text+f' tokens   ·   Total in: {number(total_in)}   ·   Total out: {number(total_out)}'+(' (known usage only)' if unknown else '')+'   ·   '+s['connection_name'])
    def closeEvent(self,event):
        running = [self.tabs.widget(i) for i in range(self.tabs.count()) if isinstance(self.tabs.widget(i),SessionTab) and self.tabs.widget(i).job]
        if running and not confirm(self,'Requests are running. Close Promptnik and stop receiving responses?'): event.ignore(); return
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab,SessionTab): tab.cancel(); tab.save_draft(); tab._closed = True
        event.accept()

def main():
    parser = argparse.ArgumentParser(description='Promptnik desktop chat and System One client')
    parser.add_argument('--data-dir',type=Path,help='Override the application data directory')
    parser.add_argument('--litellm-url',help='Expose a LiteLLM connection for this launch')
    parser.add_argument('--litellm-key-file',type=Path,help='Read the LiteLLM API key without putting it on the command line')
    parser.add_argument('--ollama-key-file',type=Path,help='Expose an Ollama Cloud connection using a key file')
    args = parser.parse_args()
    if args.litellm_url: os.environ['LITELLM_BASE_URL'] = args.litellm_url
    try:
        if args.litellm_key_file: os.environ['LITELLM_API_KEY'] = args.litellm_key_file.read_text('utf-8-sig').strip()
        if args.ollama_key_file:
            os.environ['OLLAMA_API_KEY'] = args.ollama_key_file.read_text('utf-8-sig').strip(); os.environ['OLLAMA_BASE_URL'] = 'https://ollama.com/v1'
            # Avoid an inherited host overriding the explicit Cloud connection.
            os.environ.pop('OLLAMA_HOST',None)
    except OSError as exc: parser.error(f'Cannot read key file: {exc}')
    app = QApplication(sys.argv); app.setApplicationName('Promptnik'); app.setOrganizationName('Promptnik'); app.setStyle('Fusion')
    try: window = Window(Store(args.data_dir))
    except Exception as exc: error(None,exc); return
    window.show(); sys.exit(app.exec())
