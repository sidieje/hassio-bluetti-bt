#!/bin/sh

# py313 matches the interpreter in requirements-based CI and Dockerfile.test.
black --target-version py313 .
