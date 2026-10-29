"""Bounded word-coverage checks for generated speech, not semantic verification.

Recognition can make mistakes: this catches clear truncation and substantial
omissions, not every missing word, paraphrase, mispronunciation or emotion.
"""
from array import array
from dataclasses import dataclass
import math
import re
import unicodedata

import numpy as np


MAX_PCM_BYTES = 8 * 1024 * 1024
MAX_EXPECTED_WORDS = 600
MAX_HEARD_WORDS = 1500
MAX_TEXT_CHARACTERS = 100_000
_SPEAKER = re.compile(r'<\|speaker:\d+\|>', re.I)
_DELIVERY = re.compile(r'\[[^\]]*\]')
_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*", re.UNICODE)
_FILLERS = frozenset(('um','uh','erm','hmm','hm','ah'))
_CONTRACTIONS = {
    "i'm":'i am', "you're":'you are', "we're":'we are', "they're":'they are',
    "i've":'i have', "you've":'you have', "we've":'we have', "they've":'they have',
    "i'll":'i will', "you'll":'you will', "we'll":'we will', "they'll":'they will',
    "he'll":'he will', "she'll":'she will', "it'll":'it will',
    "i'd":'i would', "you'd":'you would', "we'd":'we would', "they'd":'they would',
    "he'd":'he would', "she'd":'she would',
    "he's":'he is', "she's":'she is', "it's":'it is', "that's":'that is',
    "there's":'there is', "here's":'here is', "what's":'what is',
    "who's":'who is', "where's":'where is', "how's":'how is', "let's":'let us',
    "can't":'can not', 'cannot':'can not', "won't":'will not', "shan't":'shall not',
    "don't":'do not', "doesn't":'does not', "didn't":'did not',
    "isn't":'is not', "aren't":'are not', "wasn't":'was not', "weren't":'were not',
    "haven't":'have not', "hasn't":'has not', "hadn't":'had not',
    "wouldn't":'would not', "shouldn't":'should not', "couldn't":'could not',
    "mustn't":'must not', "needn't":'need not',
}
_SMALL = ('zero','one','two','three','four','five','six','seven','eight','nine',
          'ten','eleven','twelve','thirteen','fourteen','fifteen','sixteen',
          'seventeen','eighteen','nineteen')
_TENS = ('','','twenty','thirty','forty','fifty','sixty','seventy','eighty','ninety')


@dataclass(frozen=True)
class CoverageReport:
    acceptable: bool
    expected_words: int
    heard_words: int
    matched_words: int
    coverage: float
    reason: str

    def safe_metadata(self):
        return {'acceptable':self.acceptable, 'expected_words':self.expected_words,
                'heard_words':self.heard_words, 'matched_words':self.matched_words,
                'coverage':round(self.coverage,4), 'reason':self.reason}


def spoken_text(text):
    if not isinstance(text,str) or len(text)>MAX_TEXT_CHARACTERS:
        raise ValueError('Use bounded plain text for speech comparison.')
    return ' '.join(_DELIVERY.sub(' ',_SPEAKER.sub(' ',text)).split())


def resample_for_recognition(pcm44100):
    """Convert bounded native-rate mono PCM16 to the recognizer's 16 kHz input."""
    if not isinstance(pcm44100,(bytes,bytearray,memoryview)):
        raise ValueError('Use nonempty, aligned 44100 Hz mono PCM16 audio.')
    size = pcm44100.nbytes if isinstance(pcm44100,memoryview) else len(pcm44100)
    if not size or size%2 or size>MAX_PCM_BYTES:
        raise ValueError('Use at most 8 MiB of nonempty, aligned mono PCM16 audio.')
    samples = np.frombuffer(bytes(pcm44100),dtype='<i2').astype(np.float64)
    count = max(1,round(len(samples)*16000/44100))
    # A short low-pass filter reduces aliasing before interpolation. Constant
    # padding preserves a signal's start/end and avoids introducing silence.
    if len(samples)>=63:
        positions = np.arange(63)-31
        cutoff = 7200/44100
        kernel = 2*cutoff*np.sinc(2*cutoff*positions)*np.hamming(63)
        kernel /= kernel.sum()
        samples = np.convolve(np.pad(samples,(31,31),mode='edge'),kernel,mode='valid')
    positions = np.arange(count,dtype=np.float64)*(44100/16000)
    output = np.interp(positions,np.arange(len(samples)),samples)
    return np.clip(np.rint(output),-32768,32767).astype('<i2').tobytes()


def _number_words(number):
    if number<20: return [_SMALL[number]]
    if number<100:
        return [_TENS[number//10]]+([_SMALL[number%10]] if number%10 else [])
    if number<1000:
        return [_SMALL[number//100],'hundred']+(_number_words(number%100) if number%100 else [])
    return [str(number)]


def _words(text):
    text = unicodedata.normalize('NFKC',spoken_text(text)).casefold()
    text = text.translate(str.maketrans({'’':"'",'‘':"'",'ʼ':"'",'`':"'",'%':' percent '}))
    text = ''.join(character for character in unicodedata.normalize('NFKD',text)
                   if not unicodedata.combining(character))
    text = re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))','',text)
    text = re.sub(r'\b([0-9]{1,3})\.([0-9]{1,3})\b',
                  lambda match:' '.join(_number_words(int(match[1]))+['point']+
                                        [_SMALL[int(digit)] for digit in match[2]]),text)
    words = []
    for match in _WORD.finditer(text):
        word = match.group()
        expanded = _CONTRACTIONS.get(word,word).split()
        for item in expanded:
            if item in _FILLERS: continue
            if re.fullmatch(r'[0-9]{1,3}',item): words.extend(_number_words(int(item)))
            else: words.append(item)
    return words


def _ordered_matches(expected,heard):
    """LCS with bounded O(n*m) work and a compact uint16 alignment table."""
    rows = [array('H',[0])*(len(heard)+1)]
    for word in expected:
        previous = rows[-1]
        row = array('H',[0])*(len(heard)+1)
        for index,actual in enumerate(heard,1):
            row[index] = previous[index-1]+1 if word==actual else max(previous[index],row[index-1])
        rows.append(row)
    left,right = len(expected),len(heard)
    positions = []
    while left and right:
        if expected[left-1]==heard[right-1]:
            positions.append(left-1);left-=1;right-=1
        elif rows[left-1][right]>=rows[left][right-1]: left-=1
        else: right-=1
    return int(rows[-1][-1]),positions


def normalized_prefix(expected, heard):
    """Whether recognized lexical words are an actual initial expected sequence."""
    expected, heard = _words(expected), _words(heard)
    if len(expected)>MAX_EXPECTED_WORDS or len(heard)>MAX_HEARD_WORDS:
        raise ValueError('Speech comparison exceeds its bounded word limit.')
    return bool(heard) and len(heard)<=len(expected) and expected[:len(heard)]==heard


def compare_speech(expected,heard):
    """Compare ordered word overlap; this does not establish semantic accuracy."""
    expected = _words(expected)
    heard = _words(heard)
    size,actual = len(expected),len(heard)
    if size>MAX_EXPECTED_WORDS or actual>MAX_HEARD_WORDS:
        raise ValueError('Speech comparison exceeds its bounded word limit.')
    if not size: return CoverageReport(False,size,actual,0,0.0,'no_expected_words')
    if not actual: return CoverageReport(False,size,actual,0,0.0,'no_recognized_words')
    matched,positions = _ordered_matches(expected,heard)
    coverage = matched/size
    acceptable,reason = True,'covered'
    if size>=8:
        if coverage<.70:
            acceptable,reason = False,'insufficient_word_coverage'
        elif actual>size+5 and matched/actual<.65:
            acceptable,reason = False,'excess_repeated_speech'
        elif (size>=12 and actual<=size*.85 and positions
              and size-1-max(positions)>=max(5,math.ceil(size*.15))):
            acceptable,reason = False,'missing_reply_end'
    # Short replies provide little evidence. Permit a minor recognition error,
    # but reject an empty/unrelated transcript or a clearly tiny fragment.
    elif matched<max(1,size-2) and coverage<.5:
        acceptable,reason = False,'short_reply_not_covered'
    return CoverageReport(acceptable,size,actual,matched,coverage,reason)
