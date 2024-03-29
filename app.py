import numpy as np
import subprocess
import os 
import stat
import signal
import sys
import pyaudio
from six.moves import queue
import re
from google.cloud import speech


# Audio recording parameters
RATE = 16000
CHUNK = int(RATE / 10)  # 100ms

class PipeStream(object):
    """Opens a recording stream as a generator yielding the audio chunks."""

    def __init__(self, rate, chunk, pipe):
        self._rate = rate
        self._chunk = chunk
        self.pipe = pipe

        # Create a thread-safe buffer of audio data
        self._buff = queue.Queue()
        self.closed = True

    def __enter__(self):
        self._audio_interface = pyaudio.PyAudio()
        self._audio_stream = self._audio_interface.open(
            format=pyaudio.paInt16,
            # The API currently only supports 1-channel (mono) audio
            # https://goo.gl/z757pE
            channels=1,
            rate=self._rate,
            input=True,
            frames_per_buffer=self._chunk,
            # Run the audio stream asynchronously to fill the buffer object.
            # This is necessary so that the input device's buffer doesn't
            # overflow while the calling thread makes network requests, etc.
            stream_callback=self._fill_buffer,
        )

        self.closed = False

        return self

    def __exit__(self, type, value, traceback):
        self._audio_stream.stop_stream()
        self._audio_stream.close()
        self.closed = True
        # Signal the generator to terminate so that the client's
        # streaming_recognize method will not block the process termination.
        self._buff.put(None)
        self._audio_interface.terminate()

    def _fill_buffer(self, in_data, frame_count, time_info, status_flags):
        """Continuously collect data from the audio stream, into the buffer."""
        
        #with open('rtmpOutputPipe') as fifo:
        data = self.pipe.read(1024*4)
        self._buff.put(np.frombuffer(data, np.int16))
        return None, pyaudio.paContinue

    def generator(self):
        while not self.closed:
            # Use a blocking get() to ensure there's at least one chunk of
            # data, and stop iteration if the chunk is None, indicating the
            # end of the audio stream.
            chunk = self._buff.get()
            if chunk is None:
                return
            data = [chunk]

            # Now consume whatever other data's still buffered.
            while True:
                try:
                    chunk = self._buff.get(block=False)
                    if chunk is None:
                        return
                    data.append(chunk)
                except queue.Empty:
                    break

            yield b"".join(data)
            
def listen_print_loop(responses):
    """Iterates through server responses and prints them.

    The responses passed is a generator that will block until a response
    is provided by the server.

    Each response may contain multiple results, and each result may contain
    multiple alternatives; for details, see https://goo.gl/tjCPAU.  Here we
    print only the transcription for the top alternative of the top result.

    In this case, responses are provided for interim results as well. If the
    response is an interim one, print a line feed at the end of it, to allow
    the next result to overwrite it, until the response is a final one. For the
    final one, print a newline to preserve the finalized transcription.
    """
    num_chars_printed = 0
    for response in responses:
        if not response.results:
            continue

        # The `results` list is consecutive. For streaming, we only care about
        # the first result being considered, since once it's `is_final`, it
        # moves on to considering the next utterance.
        result = response.results[0]
        if not result.alternatives:
            continue

        # Display the transcription of the top alternative.
        transcript = result.alternatives[0].transcript

        # In order to access the timing of the transcription in relevance to the start of the stream use the following 
        # object returned as part of the result
        # result.result_end_time       

        # Display interim results, but with a carriage return at the end of the
        # line, so subsequent lines will overwrite them.
        #
        # If the previous result was longer than this one, we need to print
        # some extra spaces to overwrite the previous result
        overwrite_chars = " " * (num_chars_printed - len(transcript))

        if not result.is_final:
            sys.stdout.write(transcript + overwrite_chars + "\r")
            sys.stdout.flush()

            num_chars_printed = len(transcript)

        else:
            print(transcript + overwrite_chars)

            # Exit recognition if any of the transcribed phrases could be
            # one of our keywords.
            if re.search(r"\b(exit|quit)\b", transcript, re.I):
                print("Exiting..")
                break

            num_chars_printed = 0

def main(pipe):
    # See http://g.co/cloud/speech/docs/languages
    # for a list of supported languages.
    language_code = "en-US"  # a BCP-47 language tag

    client = speech.SpeechClient()
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=RATE,
        language_code=language_code,
    )

    streaming_config = speech.StreamingRecognitionConfig(
        config=config, interim_results=True
    )
    with PipeStream(RATE, CHUNK, pipe) as stream:
        audio_generator = stream.generator()
        requests = (
            speech.StreamingRecognizeRequest(audio_content=content)
            for content in audio_generator
        )

        responses = client.streaming_recognize(streaming_config, requests)

        # Now, put the transcription responses to use.
        listen_print_loop(responses)
        

def startFfmpegSubProcess(rtmpUrl, outputPipe):
  subprocess.call('ffmpeg -i '+rtmpUrl+' -vn -c:a pcm_s16le -ar 16000 -ac 1 -y -f s16le -hide_banner -loglevel error '+outputPipe+' &', shell=True)
  #subprocess.call('ffmpeg -i '+rtmpUrl+' -vn -c:a pcm_s16le -ar 16000 -ac 1 -y -loglevel debug  -f wav '+outputPipe+' &', shell=True)

if __name__ == "__main__":
  mode = False
  if(os.path.exists('rtmpOutputPipe')):
    mode = os.stat('rtmpOutputPipe').st_mode
    
  if(stat.S_ISFIFO(mode) == False):
    try:
      print("creating the FIFO pipe")
      os.mkfifo("rtmpOutputPipe")
    except :
        print("Failed to create the FIFO pipe")

  print("starting FFMPEG to capture audio stream from RTMP")
  startFfmpegSubProcess('rtmp://127.0.0.1/live/ByzY5OGkc', 'rtmpOutputPipe')
  
  pipe = open('rtmpOutputPipe', 'rb')
  
  print("Transcoding extracted audio stream")
  main(pipe)
