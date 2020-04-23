# syntax=docker/dockerfile:1.0.0-experimental
FROM continuumio/miniconda3

ENV LANG=C.UTF-8 LC_ALL=C.UTF-8
ENV HOME=/root
WORKDIR $HOME

RUN apt-get update && apt-get -y install gcc

COPY requirements.txt $HOME/
RUN pip install -r $HOME/requirements.txt

COPY . $HOME/
RUN pip install -e $HOME/gluon-ts
RUN pip install -e ".[dev]"
