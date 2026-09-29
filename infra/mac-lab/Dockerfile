# The launcher resolves the official Python image to a digest before building.
ARG LAB_PYTHON_IMAGE
FROM ${LAB_PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN mkdir -p /opt/lab /data && chown 10001:10001 /data
WORKDIR /opt/lab
COPY --chown=10001:10001 runtime.py /opt/lab/runtime.py
COPY --chown=10001:10001 tests /opt/lab/tests
USER 10001:10001
ENTRYPOINT ["python", "/opt/lab/runtime.py"]
CMD ["serve", "--data", "/data"]
