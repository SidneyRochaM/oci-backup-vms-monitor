FROM fnproject/python:3.11-dev as build-stage
WORKDIR /function
ADD requirements.txt /function/

# --- MUDANÇA: Instala tudo na própria pasta /function para evitar erro de PATH ---
RUN pip3 install --target /function/ --no-cache --no-cache-dir -r requirements.txt &&\
    rm -fr ~/.cache/pip /tmp* requirements.txt func.yaml Dockerfile .venv

ADD . /function/
RUN rm -fr /function/.pip_cache

FROM fnproject/python:3.11
WORKDIR /function

# Copia tudo de uma vez (libs + seu código)
COPY --from=build-stage /function /function

# Garante que o Python enxergue a pasta atual
ENV PYTHONPATH=/function

# Define permissão de execução (segurança extra)
RUN chmod +x /function/bin/fdk

# O binário do fdk agora está dentro de /function/bin
ENTRYPOINT ["/function/bin/fdk", "/function/func.py", "handler"]