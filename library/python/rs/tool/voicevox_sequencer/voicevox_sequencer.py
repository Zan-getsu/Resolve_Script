import dataclasses
import io
import sys
from pathlib import Path

import scipy as sp
import simpleaudio

from PySide6.QtCore import (
    Qt,
    QItemSelectionModel,
)
from PySide6.QtGui import QColor, QUndoStack
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMenu,
)

from rs.core import (
    config,
    pipe as p,
    voicevox,
)
from rs.core.voicevox.data import SpeakerList
from rs.core.voicevox.api import synthesis
from rs.gui import (
    appearance, log,
)

from rs.tool.voicevox_sequencer import seq
from rs.tool.voicevox_sequencer.voicevox_sequencer_ui import Ui_MainWindow

APP_NAME = 'VoicevoxSequencer'


@dataclasses.dataclass
class ConfigData(config.Data):
    speaker_index: int = 0


@dataclasses.dataclass
class Doc(config.Data):
    speaker_id: int = 0
    tempo: int = 120
    note_list: config.DataList = dataclasses.field(default_factory=lambda: config.DataList(seq.NoteData))


def paragraph2accent_phrases(
        paragraph: seq.Paragraph, tempo: int
) -> list[voicevox.data.AccentPhrase]:
    moras = []
    accent_phrases = []
    for note in paragraph.note_list:
        mora = voicevox.data.Mora()
        length = note.get_sec(tempo)
        max_sec = note.max_time
        mora.set_note(
            note.kana,
            note.note,
            min(length, max_sec),
        )
        moras.append(mora)

        if note.get_sec(tempo) > max_sec:
            accent_phrase = voicevox.data.AccentPhrase()
            pause_mora = voicevox.data.Mora()
            pause_mora.set_rest(length - max_sec)
            accent_phrase.pause_mora = pause_mora
            accent_phrase.moras.set_list(moras)
            accent_phrases.append(accent_phrase)
            moras = []
    if len(moras) > 0:
        if paragraph.rest_length > 0:
            mora = voicevox.data.Mora()
            mora.set_rest(paragraph.rest_length)
            moras.append(mora)
        accent_phrase = voicevox.data.AccentPhrase()
        accent_phrase.moras.set_list(moras)
        accent_phrases.append(accent_phrase)
    else:
        if paragraph.rest_length > 0:
            mora = accent_phrases[-1].pause_mora
            rest_length = 0
            if mora is None:
                mora = voicevox.data.Mora()
            else:
                rest_length = mora.vowel_length
            note = seq.NoteData(
                note=-1,
                length=paragraph.rest_length,
                max_time=0.5,
                kana='、',
            )
            note.get_sec(tempo)
            mora.set_rest(rest_length + note.get_sec(tempo))
            accent_phrases[-1].pause_mora = mora
    return accent_phrases


class MainWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        self.file = None
        self.set_title()
        self.setWindowFlags(
            Qt.Window
        )
        self.resize(500, 800)

        # header
        self.speaker_list = SpeakerList()
        self.speakers_file: Path = config.CONFIG_DIR.joinpath('voicevox_speakers.json')
        if self.speakers_file.is_file():
            self.speaker_list.load(self.speakers_file)
        self.set_speaker_list()
        self.ui.tempoSpinBox.setValue(120)

        # config
        self.config_file: Path = config.CONFIG_DIR.joinpath('%s.json' % APP_NAME)
        self.load_config()

        # style sheet
        self.ui.saveButton.setStyleSheet(appearance.ex_stylesheet)
        self.ui.playButton.setStyleSheet(appearance.other_stylesheet)
        self.ui.stopButton.setStyleSheet(appearance.other_stylesheet)
        self.ui.getSpeakerButton.setStyleSheet(appearance.other_stylesheet)

        # table
        v = self.ui.tableView
        self.undo_stack: QUndoStack = v.undo_stack

        v.setContextMenuPolicy(Qt.CustomContextMenu)
        v.customContextMenuRequested.connect(self.contextMenu)

        self.new_doc()
        # thread
        # self.player_thread = None
        # self.player = None

        # event
        self.undo_stack.cleanChanged.connect(self.set_title)

        self.ui.getSpeakerButton.clicked.connect(self.get_speakers)

        self.ui.playButton.clicked.connect(self.play)
        # self.ui.stopButton.clicked.connect(self.stop)
        # self.ui.saveButton.clicked.connect(self.wave_save)
        self.ui.closeButton.clicked.connect(self.close)
        #
        self.ui.actionNew.triggered.connect(self.new_doc)
        self.ui.actionOpen.triggered.connect(self.open_doc)
        self.ui.actionSave.triggered.connect(self.save_doc)
        self.ui.actionSave_As.triggered.connect(self.save_as_doc)
        self.ui.actionExit.triggered.connect(self.close)

        self.ui.actionUndo.triggered.connect(self.undo_stack.undo)
        self.ui.actionRedo.triggered.connect(self.undo_stack.redo)

        self.ui.actionEdit.triggered.connect(self.edit)
        self.ui.actionAdd.triggered.connect(self.add)
        self.ui.actionIncrement.triggered.connect(self.ui.tableView.increment)
        self.ui.actionIncrementPlus.triggered.connect(self.ui.tableView.increment_plus)
        self.ui.actionDecrement.triggered.connect(self.ui.tableView.decrement)
        self.ui.actionDecrementPlus.triggered.connect(self.ui.tableView.decrement_plus)
        self.ui.actionClear.triggered.connect(self.ui.tableView.clear)
        self.ui.actionCopy.triggered.connect(self.ui.tableView.copy)
        self.ui.actionPaste.triggered.connect(self.ui.tableView.paste)
        self.ui.actionDelete.triggered.connect(self.ui.tableView.delete)
        self.ui.actionUp.triggered.connect(self.ui.tableView.up)
        self.ui.actionDown.triggered.connect(self.ui.tableView.down)
        #
        # self.ui.actionPlay.triggered.connect(self.play_or_stop)
        # self.ui.actionWav_Save.triggered.connect(self.wave_save)

    def set_title(self):
        if self.file is None:
            self.setWindowTitle('%s' % APP_NAME)
        else:
            star = '*' if self.ui.tableView.model().undo_stack.isClean() is False else ''
            self.setWindowTitle('%s - %s%s' % (APP_NAME, self.file, star))

    def play(self):
        v = self.ui.tableView
        doc = self.get_data()

        # make query
        paragraph_list = v.get_paragraph_list()
        query_list = []
        for paragraph in paragraph_list:
            accent_phrases = paragraph2accent_phrases(
                paragraph, doc.tempo
            )
            query = voicevox.data.AudioQuery()
            query.accent_phrases.set_list(accent_phrases)
            query_list.append(query)

        from pprint import pprint
        self.log_clear()
        data_list = []
        for query in query_list:
            self.add_log('Synthesis...')
            try:
                audio = synthesis(doc.speaker_id, query.as_dict(), 5)
                fs, data = sp.io.wavfile.read(io.BytesIO(audio))
                data_list.append((fs, data))
            except Exception as e:
                self.add_error(f'Error: {e}')
                self.add_log('Failed.')
                return
        self.add_log('Play')
        for data in data_list:
            play_obj = simpleaudio.play_buffer(data[1], 1, 2, data[0])
            play_obj.wait_done()

    def play_state_off(self):
        self.is_playing = False

    def play_or_stop(self):
        if self.is_playing:
            self.stop()
        else:
            self.play()

    def stop(self):
        pass

    def wave_save(self):
        pass

    def edit(self):
        v = self.ui.tableView
        v.edit(v.currentIndex())

    def contextMenu(self, pos):
        v = self.ui.tableView
        menu = QMenu(v)
        menu.addAction(self.ui.actionUndo)
        menu.addAction(self.ui.actionRedo)
        menu.addSeparator()
        menu.addAction(self.ui.actionEdit)
        menu.addSeparator()
        menu.addAction(self.ui.actionIncrement)
        menu.addAction(self.ui.actionIncrementPlus)
        menu.addAction(self.ui.actionDecrement)
        menu.addAction(self.ui.actionDecrementPlus)
        menu.addSeparator()
        menu.addAction(self.ui.actionCopy)
        menu.addAction(self.ui.actionPaste)
        menu.addSeparator()
        menu.addAction(self.ui.actionAdd)
        menu.addAction(self.ui.actionDelete)
        menu.addSeparator()
        menu.addAction(self.ui.actionUp)
        menu.addAction(self.ui.actionDown)
        menu.exec(v.mapToGlobal(pos))

    def add(self):
        v = self.ui.tableView
        m: seq.Model = v.model()
        sm = v.selectionModel()
        row = v.currentIndex().row()
        d = seq.NoteData()
        if row < 0:
            m.add_row_data(d)
        else:
            current_index = v.currentIndex()
            m.insert_row_data(row + 1, d)
            sm.setCurrentIndex(
                current_index.siblingAtRow(row + 1),
                QItemSelectionModel.SelectionFlag.ClearAndSelect
            )

    def add_log(self, text: str, color: QColor = log.TEXT_COLOR) -> None:
        self.ui.logTextEdit.log(text, color)

    def add_error(self, text: str) -> None:
        self.ui.logTextEdit.log(text, log.ERROR_COLOR)

    def log_clear(self) -> None:
        self.ui.logTextEdit.clear()

    def set_speaker_list(self) -> None:
        self.ui.speakerComboBox.clear()
        self.ui.speakerComboBox.addItems(self.speaker_list.get_display_name_list())

    def get_speakers(self):
        self.log_clear()
        self.add_log('Get speakers...')
        try:
            self.speaker_list.set_from_voicevox()
        except Exception as e:
            self.add_error(f'Error: {e}')
            self.add_log('Failed.')
            return
        self.set_speaker_list()
        self.save_speakers()
        self.add_log('Done.')

    def set_data(self, doc: Doc):
        # id
        display_name = self.speaker_list.get_display_name(doc.speaker_id)
        if display_name is not None:
            self.ui.speakerComboBox.setCurrentText(display_name)
        pass
        # tempo
        self.ui.tempoSpinBox.setValue(doc.tempo)
        # note
        v = self.ui.tableView
        m: seq.Model = v.model()
        m.set_data(doc.note_list)

    def get_data(self) -> Doc:
        doc = Doc()
        # id
        speaker_id = self.speaker_list.get_id_from_display_name(
            self.ui.speakerComboBox.currentText()
        )
        if speaker_id is not None:
            doc.speaker_id = speaker_id
        # tempo
        doc.tempo = self.ui.tempoSpinBox.value()
        # note
        v = self.ui.tableView
        m: seq.Model = v.model()
        doc.note_list.set_list(m.to_list())
        return doc

    def new_doc(self):
        self.file = None
        v = self.ui.tableView
        m: seq.Model = v.model()
        m.clear()
        self.add()
        self.undo_stack.clear()
        self.set_title()

    def open_doc(self):
        dir_path = ''
        if self.file is not None:
            dir_path = Path(self.file).parent
        path, _ = QFileDialog.getOpenFileName(
            self,
            'Open File',
            str(dir_path),
            'JSON File (*.json);;All File (*.*)'
        )
        if path != '':
            file_path = Path(path)
            if file_path.is_file():
                a = self.get_data()
                a.load(file_path)
                self.set_data(a)
                self.log_clear()
                self.add_log('Open: %s' % str(file_path))
                self.file = str(file_path)
                self.undo_stack.clear()
                self.set_title()

    def save_doc(self):
        if self.file is None:
            self.save_as_doc()
            return
        file_path = Path(self.file)
        doc = self.get_data()
        doc.save(file_path)
        self.log_clear()
        self.add_log('Save: %s' % str(file_path))
        self.undo_stack.setClean()
        self.set_title()

    def save_as_doc(self):
        dir_path = ''
        if self.file is not None:
            dir_path = Path(self.file).parent
        path, _ = QFileDialog.getSaveFileName(
            self,
            'Save File',
            str(dir_path),
            'JSON File (*.json);;All File (*.*)'
        )
        if path != '':
            file_path = Path(path)
            doc = self.get_data()
            doc.save(file_path)
            self.log_clear()
            self.add_log('Save: %s' % str(file_path))
            self.file = str(file_path)
            self.undo_stack.setClean()
            self.set_title()

    def set_config(self, c: ConfigData):
        display_name = self.speaker_list.get_display_name(c.speaker_index)
        if display_name is not None:
            self.ui.speakerComboBox.setCurrentText(display_name)
        pass

    def get_config(self) -> ConfigData:
        c = ConfigData()
        speaker_id = self.speaker_list.get_id_from_display_name(
            self.ui.speakerComboBox.currentText()
        )
        if speaker_id is not None:
            c.speaker_index = speaker_id
        return c

    def load_config(self) -> None:
        c = ConfigData()
        if self.config_file.is_file():
            c.load(self.config_file)
        self.set_config(c)

    def save_config(self) -> None:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        c = self.get_config()
        c.save(self.config_file)
        pass

    def save_speakers(self) -> None:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.speaker_list.save(self.speakers_file)
        pass

    def closeEvent(self, event):
        self.save_config()
        self.stop()
        super().closeEvent(event)


def run() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setPalette(appearance.palette)
    app.setStyleSheet(appearance.stylesheet)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    run()
