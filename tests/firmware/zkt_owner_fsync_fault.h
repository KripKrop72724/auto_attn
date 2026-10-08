#pragma once
#include <unistd.h>
int zkt_owner_test_fsync(int descriptor);
#define fsync zkt_owner_test_fsync
