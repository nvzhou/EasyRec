#!/usr/bin/env bash

# pip install
pip install oss2
pip install -r requirements.txt

# update/generate proto
bash scripts/gen_proto.sh

export CUDA_VISIBLE_DEVICES=""
export TEST_DEVICES=""

if [[ $# -eq 1 ]]; then
  export TEST_DIR=$1
else
  export TEST_DIR="/tmp/easy_rec_test_${USER}_`date +%s`"
fi

# run test
PYTHONPATH=. python easy_rec/python/test/run.py --pattern hpo_test.*
if [ $? -eq 0 ]
then
    echo "::set-output name=ci_test_passed::1"
else
    echo "::set-output name=ci_test_passed::0"
fi
