#!/bin/bash

echo CPU_THREADS=${CPU_THREADS} >> /etc/environment

exec $@

