#!/bin/bash
# Monitor docker build progress

LOG="/tmp/build_test.log"
STATUS="/tmp/build_status.txt"

echo "=== Docker Build Status ===" > $STATUS
date >> $STATUS
echo "" >> $STATUS

if ps aux | grep -q "[d]ocker build"; then
    echo "Status: BUILDING (docker process active)" >> $STATUS
else
    echo "Status: COMPLETE or FAILED" >> $STATUS
fi

echo "" >> $STATUS
echo "Last 20 lines of build log:" >> $STATUS
tail -20 $LOG >> $STATUS 2>&1

echo "" >> $STATUS
echo "---" >> $STATUS

# Check if image exists
if docker images | grep -q "test-python313"; then
    echo "✓ Image built successfully" >> $STATUS
    docker images test-python313 >> $STATUS
    echo "" >> $STATUS
    echo "Testing imports..." >> $STATUS
    docker run --rm test-python313 >> $STATUS 2>&1
else
    echo "✗ Image not built yet or failed" >> $STATUS
fi

cat $STATUS
