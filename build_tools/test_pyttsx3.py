# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"C:\Users\李语晨\Desktop\班级叫号器")
import app

app._prepare_pyttsx3_env()
import pyttsx3
from pyttsx3.drivers import sapi5

orig = sapi5.SAPI5Driver.getProperty


def patched_get(self, name):
    if name == "voice":
        try:
            return self._tts.Voice.Id
        except Exception:
            toks = self._tts.GetVoices()
            if not toks or not toks.Count:
                raise
            for i in range(toks.Count):
                tok = toks.Item(i)
                tid = str(tok.Id or "").lower()
                lang = str(tok.GetAttribute("Language") or "").lower()
                if "zh" in lang or "chinese" in tid or "huihui" in tid:
                    return tok.Id
            return toks.Item(0).Id
    return orig(self, name)


sapi5.SAPI5Driver.getProperty = patched_get

engine = pyttsx3.init()
voices = engine.getProperty("voices")
print("init ok, voices:", len(voices))
for i, v in enumerate(voices):
    print(i, v.id, "|", v.name)
engine.stop()
print("done")
