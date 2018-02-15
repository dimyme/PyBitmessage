import glob
import os

from qtpy import QtCore, QtWidgets

import paths
from bmconfigparser import BMConfigParser
from tr import _translate


class LanguageBox(QtWidgets.QComboBox):
    languageName = {
        "system": "System Settings",
        "eo": "Esperanto",
        "en_pirate": "Pirate English"
    }

    def __init__(self, parent=None):
        super(LanguageBox, self).__init__(parent)
        self.populate()

    def populate(self):
        self.clear()
        localesPath = os.path.join(paths.codePath(), 'translations')
        self.addItem(
            _translate("settingsDialog", "System Settings", "system"),
            "system"
        )
        self.setCurrentIndex(0)
        self.setInsertPolicy(QtWidgets.QComboBox.InsertAlphabetically)
        for translationFile in sorted(
            glob.glob(os.path.join(localesPath, "bitmessage_*.qm"))
        ):
            localeShort = \
                os.path.split(translationFile)[1].split("_", 1)[1][:-3]
            locale = QtCore.QLocale(localeShort)
            if localeShort in LanguageBox.languageName:
                self.addItem(
                    LanguageBox.languageName[localeShort], localeShort)
            elif locale.nativeLanguageName() == "":
                self.addItem(localeShort, localeShort)
            else:
                self.addItem(locale.nativeLanguageName(), localeShort)

        configuredLocale = BMConfigParser().safeGet(
            'bitmessagesettings', 'userlocale', 'system')
        for i in range(self.count()):
            if self.itemData(i) == configuredLocale:
                self.setCurrentIndex(i)
                break
