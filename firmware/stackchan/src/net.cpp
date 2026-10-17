#include "net.h"

#include "root_ca.h"

void httpBegin(HTTPClient& http, const String& url) {
  if (!url.startsWith("https://")) http.begin(url);
#if HAVE_ROBOT_CERT
  else {  // mTLS at the public endpoint; this overload wants host, port and path apart
    const int slash = url.indexOf('/', 8);
    const String hostPort = slash < 0 ? url.substring(8) : url.substring(8, slash);
    const int colon = hostPort.indexOf(':');
    http.begin(colon < 0 ? hostPort : hostPort.substring(0, colon), colon < 0 ? 443 : hostPort.substring(colon + 1).toInt(),
               slash < 0 ? String("/") : url.substring(slash), KKH_ROOT_CA, ROBOT_CLIENT_CERT, ROBOT_CLIENT_KEY);
  }
#else
  else http.begin(url, KKH_ROOT_CA);
#endif
}
