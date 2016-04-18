import cherrypy
import httplib
import urllib
import logging
import json
import itertools
import os
import os.path
from textstat.textstat import textstat

### Generates a list of (English) Wikipedia pages in a given category, sorted by Flesch Reading Ease Score.

# Global read-only configuration. All API calls are made against https://wpserver/wpapi.
wpserver = "en.wikipedia.org"
wpapi = "/w/api.php"

# GET https://server/url?params, returning the result as a plain string if the response is a 200 OK
# or throwing otherwise. 'server' and 'url' should be strings; 'params' should be a dict mapping keys
# to values to build a query string (e.g. {"a": "b", "c": "d"} will become ?a=b&c=d.
#
# If we want to expand this to cope with higher traffic, use a proper connection-pooling mechanism
# such as libcURL. Twisted, or similar. 
def httpget(server, url, params):

    conn = httplib.HTTPSConnection(server)
    try:
        conn.request("GET", "%s?%s" % (url, "&".join(["%s=%s" % (k, v) for (k, v) in params.iteritems()])))
        result = conn.getresponse()
        if result.status != 200:
            raise Exception("Fetching %s from %s with arguments %s failed with code %s\n\nError document:\n%s" % 
                            (url, server, params, result.status, result.read()))
        return result.read()
    finally:
        try:
            conn.close()
        except Exception as e:
            logging.warning("Failed to close connection to %s: %s" % (server, e)) 

# Perform a Wikipedia API query against https://server/url?params. Params should be a dict that
# specifies the initial query string as given above. This function will take care of query splitting
# / continue handling, and returns an iterable of results yielded by each subquery. It throws if
# the Wikipedia API reports an error, or whenever httpget would throw.
def iterwikiquery(server, url, params):

    continueparams = {"continue": ""}
    while continueparams is not None:
        nextparams = params.copy()
        nextparams.update(continueparams)
        rawresult = httpget(server, url, nextparams)
        try:
            result = json.loads(rawresult)
        except:
            logging.warning("Failed to decode JSON '%s'" % rawresult)
            raise
        
        if "error" in result:
            raise Exception("Wiki API error: request for %s / %s / %s yielded %s" %
                            (server, url, nextparams, result["error"]))
        if "warning" in result:
            logging.warning("Wiki API warning: request for %s / %s / %s yielded %s" %
                            (server, url, nextparams, result["warning"]))
        if "continue" not in result:
            continueparams = None
        else:
            continueparams = result["continue"]
        if "query" in result:
            yield result["query"]

# Get a Flesch Reading Ease Score for 'extract', or return None if the test fails.
def trygetreadingease(extract):
    # Avoid spamming warnings for the common case of zero-length extracts:
    if len(extract.strip()) == 0:
        return None
    try: 
        return textstat.flesch_reading_ease(extract)
    except Exception as e:
        logging.warning("Can't get FRES for %s: %s" % (extract, e))
        return None

# Get the first paragraph of 'extract', which may be empty.
def getfirstparagraph(extract):
    # Wikimedia delimits paragraphs by newlines.
    return extract.split("\n", 1)[0]

# Get the first 50 extracts from English Wikipedia Category 'catname'. Return a list of tuples,
# each of which contains members:
# (page ID, page title, first paragraph, Flesh Reading Ease Score (or None if not applicable))
# WP API generators are used to minimise query traffic with the WP servers; however up to 50 queries
# may be necessary if the text-extracts extension will not allow large batches to be fetched.
# Throws whenever 'iterwikiquery' would throw.
def collectextracts(catname):

    extresults = iterwikiquery(wpserver, wpapi, {"action": "query",
                                                 "prop": "extracts",
                                                 "explaintext": "true",
                                                 "exsentences": "10",
                                                 "exlimit": "max",
                                                 "generator": "categorymembers",
                                                 "gcmtitle": "Category:%s" % catname,
                                                 "gcmtype": "page",
                                                 "gcmlimit": "50",
                                                 "format": "json"})

    result = []

    for resultpage in extresults:
        for (k, v) in resultpage["pages"].iteritems():
            if "extract" in v:
                firstpara = getfirstparagraph(v["extract"])
                result.append((k, v["title"], firstpara, trygetreadingease(firstpara)))

    return result

# Check whether Category:catname exists in English wikipedia. Returns Boolean,
# or throws if iterwikiquery would.
def checkcategoryexists(catname):
    queryresults = list(iterwikiquery(wpserver, wpapi, {"action": "query",
                                                        "titles": "Category:%s" % catname,
                                                        "format": "json"}))
    assert(len(queryresults) == 1)
    queryresult = queryresults[0]
    return "missing" not in queryresult["pages"].values()[0]

# Build a simple HTML table from iterable-of-iterables 'rowlist'. Delimit the first row with <th>
# instead of <td> tags if 'header' is set.
# Switch to a proper HTML generating library if our needs get much more sophisticated.
def makesimpletable(rowlist, header = True):
    if header:
        rowtags = itertools.chain(["th"], itertools.repeat("td", len(rowlist) - 1))
    else:
        rowtags = itertools.repeat("td", len(rowlist))
    rows = ["".join(["<%s>%s</%s>" % (rowtag, x, rowtag) for x in row]) for (row, rowtag) in itertools.izip(rowlist, rowtags)]
    return "<table>%s</table>" % "".join(["<tr>%s</tr>" % r for r in rows])

# Define a CherryPy application to host our reading ease query:
class ReadEase(object):

    # Import Github's (open) pleasing visual style.
    htmlheader = '<head><link href="/static/github-markdown.css" rel="stylesheet"></head>'

    # Index page: a simple query form.
    def index(self):
        return """<html>%s<body><form method="get" action="/getreadease">
                  <p>Get reading ease scores for category: <input length="50" name="catname"/></p>
                  <p><input type="submit"/></form></body></html>""" % ReadEase.htmlheader
    index.exposed = True

    # /getreadease?catname=X: produce a table of reading ease scores for Category:X.
    def getreadease(self, catname):

        # Escape any special characters before we go any further.
        catname = urllib.quote(catname.replace(" ", "_"))

        try:
            resultlist = collectextracts(catname)
            if len(resultlist) == 0 and not checkcategoryexists(catname):
                body = "<h4>No such category: %s</h4>" % catname
            else:
                scoredresults = filter(lambda t: t[3] is not None, resultlist)
                sortedresults = sorted(scoredresults, key = lambda t : t[3])
                tableheader = ("Page ID", "Title", "First paragraph", "Flesch Reading Ease Score")
                body = makesimpletable([tableheader] + sortedresults)
        except Exception as e:
            body = "<h4>Unexpected error querying category %s:</h4><p><pre>%s</pre></p>" % (catname, e)
        bodytemplate = "<html>%s<body><h3>Reading ease for category: %s</h3>%s</body></html>"
        return bodytemplate % (ReadEase.htmlheader, catname, body)
        
    getreadease.exposed = True
        
# Configure and start CherryPy container:
cherrypy.config.update({"server.socket_port": 8926, 
                        "server.socket_host": "0.0.0.0"})

appconf = {'/': {
            'tools.staticdir.root': os.path.abspath(os.getcwd())
          },
           '/static': {
            'tools.staticdir.on': True,
            'tools.staticdir.dir': './static'
          }}

cherrypy.quickstart(ReadEase(), '/', appconf)

