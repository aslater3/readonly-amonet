#!/bin/bash

rm -rf dist

mkdir -p dist/kamakiri/brom-payload/stage1 dist/kamakiri/brom-payload/stage2 dist/kamakiri/modules
cp bootrom-step.sh fastboot-step.sh gpt-fix.sh dist/kamakiri/
cp -r bin dist/kamakiri
cp -r brom-payload/stage1/stage1.bin dist/kamakiri/brom-payload/stage1
cp -r brom-payload/stage2/stage2.bin dist/kamakiri/brom-payload/stage2
cp -r modules/*.py dist/kamakiri/modules
cp requirements.txt dist/kamakiri
cd dist/
zip -r kamakiri-cupcake.zip *
cd ..
