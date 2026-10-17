// HTTP to the bridge on whichever network the robot is on.
#pragma once

#include <HTTPClient.h>

// https:// (the public endpoint) checks the server against the hospital certificate's roots,
// and shows the robot's own certificate when it has one (mTLS).
void httpBegin(HTTPClient& http, const String& url);
