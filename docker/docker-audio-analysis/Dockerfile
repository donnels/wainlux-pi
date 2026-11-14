FROM docker.io/library/python@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg=7:5.1.9-0+deb12u1 \
    && rm -rf /var/lib/apt/lists/*

COPY analyze_laser_audio.py /usr/local/bin/analyze_laser_audio.py
COPY timeline.js /usr/local/bin/timeline.js
COPY audio_api.py /usr/local/bin/audio_api.py
COPY test_analyze_laser_audio.py /tests/test_analyze_laser_audio.py
COPY test_audio_api.py /tests/test_audio_api.py
RUN PYTHONPATH=/usr/local/bin python3 -m unittest discover -s /tests
ENTRYPOINT ["python3", "/usr/local/bin/analyze_laser_audio.py"]