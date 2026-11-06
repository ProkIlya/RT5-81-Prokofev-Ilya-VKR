# Отдельная воспроизводимая сборка S14; исторический Dockerfile S09 не изменяется.
FROM python:3.10-slim-bookworm@sha256:db6ee5170ebce0f4d165af242e8e77addc08bd9ffc37e8b1815752cf027aa4f8 AS python_source
RUN python -I -c "import shutil,sysconfig; shutil.copytree(sysconfig.get_path('stdlib'), '/clean-stdlib', ignore=shutil.ignore_patterns('site-packages','__pycache__','test','tests','ensurepip','idlelib','tkinter'))"

FROM scratch
COPY --from=python_source /usr/local/bin/python3.10 /usr/local/bin/python
COPY --from=python_source /usr/local/lib/libpython3.10.so.1.0 /usr/local/lib/
COPY --from=python_source /clean-stdlib /usr/local/lib/python3.10/
COPY --from=python_source /lib/x86_64-linux-gnu/ /lib/x86_64-linux-gnu/
COPY --from=python_source /lib64/ /lib64/
ENV LD_LIBRARY_PATH=/usr/local/lib LANG=C.UTF-8
LABEL org.api-agent.workspace="s09d2-python310-v1"
WORKDIR /tmp
USER 65534:65534
ENTRYPOINT ["/usr/local/bin/python", "-I", "-u"]
