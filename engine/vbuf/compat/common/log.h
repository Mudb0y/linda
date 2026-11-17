// Linux stand-in for nvdaHelper/common/log.h: storage.cpp logs only at debug levels, which a
// release build of nvdaHelper compiles out too.
#ifndef NVDAHELPER_LOG_H
#define NVDAHELPER_LOG_H

#include <cassert>

#define nhAssert(x) assert(x)
#define LOG_CRITICAL(message)
#define LOG_ERROR(message)
#define LOG_WARNING(message)
#define LOG_INFO(message)
#define LOG_DEBUGWARNING(message)
#define LOG_DEBUG(message)

#endif
