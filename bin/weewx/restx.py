#    Copyright (c) 2013 Tom Keffer <tkeffer@gmail.com>
#
#    See the file LICENSE.txt for your full rights.
#
#    $Id$
#
import Queue
import datetime
import httplib
import socket
import syslog
import threading
import time
import urllib
import urllib2

import weeutil.weeutil
import weewx.wxengine
from weeutil.weeutil import to_int, to_bool, timestamp_to_string

class ServiceError(Exception):
    """Raised when not enough info is available to start a service."""
class FailedPost(IOError):
    """Raised when a post fails after trying the max number of allowed times"""
class SkippedPost(Exception):
    """Raised when a post is skipped."""
class BadLogin(StandardError):
    """Raised when login information is bad or missing."""
        
#===============================================================================
#                    Class StdRESTbase
#===============================================================================

class StdRESTbase(weewx.wxengine.StdService):
    """Base class for RESTful weewx services."""

    #
    # This class implements a generic protocol processing model:
    #
    # 1. Extract record or packet from the incoming event.
    # 2. Test whether to post.                                   Function skip_this_post
    # 3. Augment with data from database, according to protocol. Function augment_from_database
    # 4. Augment with protocol-specific entries.                 Function augment_protocol
    # 5. Format and convert to outgoing protocol.                Function format_protocol.
    # 6. Post to the appropriate queue
    #
    # All these steps are combined in function process_record.
    #

    def __init__(self, engine, config_dict, **protocol_dict):
        """Initialize StdRESTbase.
        
        Named parameters:
        
        stale: If non-None, how "fresh" a post has to be before it is accepted.
        
        interval: If non-None, how long to wait from the last post before accepting
        a new post.
        """
        super(StdRESTbase, self).__init__(engine, config_dict)
        
        self.loop_queue  = None
        self.loop_thread = None
        self.archive_queue  = None
        self.archive_thread = None
        self.stale         = protocol_dict.get('stale')
        self.post_interval = protocol_dict.get('interval')
        self.protocol      = protocol_dict.get('name', "Unknown")
        self.lastpost= None

    def init_info(self, site_dict):
        self.latitude    = float(site_dict.get('latitude',  self.engine.stn_info.latitude_f))
        self.longitude   = float(site_dict.get('longitude', self.engine.stn_info.longitude_f))
        self.hardware    = site_dict.get('station_type', self.engine.stn_info.hardware)
        self.location    = site_dict.get('location',     self.engine.stn_info.location)
        self.station_url = site_dict.get('station_url',  self.engine.stn_info.station_url)
        
    def init_loop_queue(self):
        self.loop_queue = Queue.Queue()

    def init_archive_queue(self):
        self.archive_queue = Queue.Queue()

    def shutDown(self):
        """Shut down any threads"""
        StdRESTbase.shutDown_thread(self.loop_queue, self.loop_thread)
        StdRESTbase.shutDown_thread(self.archive_queue, self.archive_thread)

    @staticmethod
    def shutDown_thread(q, t):
        if q:
            # Put a None in the queue. This will signal to the thread to shutdown
            q.put(None)
            # Wait up to 20 seconds for the thread to exit:
            t.join(20.0)
            if t.isAlive():
                syslog.syslog(syslog.LOG_ERR, "restx: Unable to shut down %s thread" % t.name)
            else:
                syslog.syslog(syslog.LOG_DEBUG, "restx: Shut down %s thread." % t.name)

    def skip_this_post(self, time_ts):
        """Check whether the post is current"""
        
        # Don't post if this record is too old
        _how_old = time.time() - time_ts
        if self.stale and _how_old > self.stale:
            raise SkippedPost("record %s is stale (%d > %d)." % \
                    (timestamp_to_string(time_ts), _how_old, self.stale))
 
        # We don't want to post more often than the post interval
        if self.lastpost and time_ts - self.lastpost < self.post_interval:
            raise SkippedPost("record %s wait interval (%d) has not passed." % \
                    (timestamp_to_string(time_ts), self.post_interval))
    
    def process_record(self, record):
        """Generic processing function that follows the protocol model."""
        
        try:
            self.skip_this_post(record['dateTime'])
        except SkippedPost, e:
            syslog.syslog(syslog.LOG_DEBUG, "restx: %s %s" % (self.protocol, e))
            return
        # Extract the record from the event, then augment it with data from the archive:
        _record = self.augment_from_database(record, self.engine.archive)
        # Then augment it with any protocol-specific data:
        self.augment_protocol(_record)
        # Format and convert to the outgoing protocol
        _request = self.format_protocol(_record)
        # Stuff it in the archive queue along with the timestamp:
        self.archive_queue.put((_record['dateTime'], _request))

    def augment_from_database(self, record, archive):
        """Augment record data with additional data from the archive.
        Returns results in the same units as the record and the database.
        
        This is a general version that works for:
          - WeatherUnderground
          - PWSweather
          - CWOP
        It can be overridden and specialized for additional protocols.

        returns: A dictionary of weather values"""
        
        _time_ts = record['dateTime']
        
        _sod_ts = weeutil.weeutil.startOfDay(_time_ts)
        
        # Make a copy of the record, then start adding to it:
        _datadict = dict(record)
        
        if not _datadict.has_key('hourRain'):
            # CWOP says rain should be "rain that fell in the past hour".  WU says
            # it should be "the accumulated rainfall in the past 60 min".
            # Presumably, this is exclusive of the archive record 60 minutes before,
            # so the SQL statement is exclusive on the left, inclusive on the right.
            _result = archive.getSql("SELECT SUM(rain), MIN(usUnits), MAX(usUnits) FROM archive WHERE dateTime>? AND dateTime<=?",
                                                   (_time_ts - 3600.0, _time_ts))
            if not _result[1] == _result[2] == record['usUnits']:
                raise ValueError("Inconsistent units or units change in database %d vs %d vs %d" % (_result[1], _result[2], record['usUnits']))
            _datadict['hourRain'] = _result[0]

        if not _datadict.has_key('rain24'):
            # Similar issue, except for last 24 hours:
            _result = archive.getSql("SELECT SUM(rain), MIN(usUnits), MAX(usUnits) FROM archive WHERE dateTime>? AND dateTime<=?",
                                                 (_time_ts - 24*3600.0, _time_ts))
            if not _result[1] == _result[2] == record['usUnits']:
                raise ValueError("Inconsistent units or units change in database %d vs %d vs %d" % (_result[1], _result[2], record['usUnits']))
            _datadict['rain24'] = _result[0]

        if not _datadict.has_key('dayRain'):
            # NB: The WU considers the archive with time stamp 00:00 (midnight) as
            # (wrongly) belonging to the current day (instead of the previous
            # day). But, it's their site, so we'll do it their way.  That means the
            # SELECT statement is inclusive on both time ends:
            _result = archive.getSql("SELECT SUM(rain), MIN(usUnits), MAX(usUnits) FROM archive WHERE dateTime>=? AND dateTime<=?", 
                                                  (_sod_ts, _time_ts))
            if not _result[1] == _result[2] == record['usUnits']:
                raise ValueError("Inconsistent units or units change in database %d vs %d vs %d" % (_result[1], _result[2], record['usUnits']))
            _datadict['dayRain'] = _result[0]
            
        return _datadict

    def augment_protocol(self, record):
        pass
    
    def format_protocol(self, record):
        raise NotImplementedError("Method 'format_protocol' not implemented")
    
#===============================================================================
#                    Class Ambient
#===============================================================================

class Ambient(StdRESTbase):
    """Base class for weather sites that use the Ambient protocol."""

    # Types and formats of the data to be published:
    _formats = {'dateTime'    : ('dateutc', lambda _v : urllib.quote(datetime.datetime.utcfromtimestamp(_v).isoformat('+'), '-+')),
                'action'      : ('action', '%s'),
                'ID'          : ('ID', '%s'),
                'PASSWORD'    : ('PASSWORD', '%s'),
                'softwaretype': ('softwaretype', '%s'),
                'barometer'   : ('baromin', '%.3f'),
                'outTemp'     : ('tempf', '%.1f'),
                'outHumidity' : ('humidity', '%03.0f'),
                'windSpeed'   : ('windspeedmph', '%03.0f'),
                'windDir'     : ('winddir', '%03.0f'),
                'windGust'    : ('windgustmph', '%03.0f'),
                'dewpoint'    : ('dewptf', '%.1f'),
                'hourRain'    : ('rainin', '%.2f'),
                'dayRain'     : ('dailyrainin', '%.2f'),
                'radiation'   : ('solarradiation', '%.2f'),
                'UV'          : ('UV', '%.2f')}

    def __init__(self, engine, config_dict, **ambient_dict):
        """Base class that implements the Ambient protocol.
        
        Named parameters:
        station: The station ID (eg. KORHOODR3) [Required]
        
        password: Password for the station [Required]
        
        name: The name of the site we are posting to. Something 
        like "Wunderground" will do. [Required]
        
        rapidfire: Set to true to have every LOOP packet post. Default is False.
        
        archive_post: Set to true to have every archive packet post. Default is 
        the opposite of rapidfire value.
        
        rapidfire_url: The base URL to be used when posting LOOP packets.
        Required if rapidfire is true.
        
        archive_url: The base URL to be used when posting archive records.
        Required if archive_post is true.
        
        log_success: Set to True if we are to log successful posts to the syslog.
        Default is false if rapidfire is true, else true.
        
        log_failure: Set to True if we are to log unsuccessful posts to the syslog.
        Default is false if rapidfire is true, else true.
        
        max_tries: The max number of tries allowed when doing archive posts.
        (Always 1 for rapidfire posts) Default is 3
        
        max_backlog: The max number of queued posts that will be allowed to accumulate.
        (Always 0 for rapidfire posts). Default is infinite.
        """
        
        super(Ambient, self).__init__(engine, config_dict, **ambient_dict)

        # Try extracting the required keywords. If this fails, an exception
        # of type KeyError will be raised. Be prepared to catch it.
        try:
            self.station = ambient_dict['station']
            self.password = ambient_dict['password']
            site_name = ambient_dict['name']
        except KeyError, e:
            # Something was missing. 
            raise ServiceError("No keyword: %s" % (e,))

        # If we got here, we have the minimum necessary.
        
        # It's not actually used by the Ambient protocol, but, for completeness,
        # initialize the site-specific information:
        self.init_info(ambient_dict)

        do_rapidfire_post = to_bool(ambient_dict.get('rapidfire', False))
        do_archive_post   = to_bool(ambient_dict.get('archive_post', not do_rapidfire_post))

        if do_rapidfire_post:
            self.rapidfire_url = ambient_dict['rapidfire_url']
            self.init_loop_queue()
            self.bind(weewx.NEW_LOOP_PACKET, self.new_loop_packet)
            ambient_dict.setdefault('log_success', False)
            ambient_dict.setdefault('log_failure', False)
            ambient_dict.setdefault('max_tries',   1)
            ambient_dict.setdefault('max_backlog', 0)
            self.loop_thread = PostRequest(self.loop_queue, 
                                           site_name + '-Rapidfire',
                                           **ambient_dict)
            self.loop_thread.start()
        if do_archive_post:
            self.archive_url = ambient_dict['archive_url']
            self.init_archive_queue()
            self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)
            self.archive_thread = PostRequest(self.archive_queue,
                                              site_name,
                                               **ambient_dict)
            self.archive_thread.start()

    def new_loop_packet(self, event):
        """Process a new LOOP event."""
        # Ambient loop posts can almost follow the standard protocol model.
        # The only difference is we have to add the keywords 'realtime' and 'rtfreq'.
        
        try:
            self.skip_this_post(event.packet['dateTime'])
        except SkippedPost, e:
            syslog.syslog(syslog.LOG_DEBUG, "restx: %s" % e)
            return

        # Extract the record from the event, then augment it with data from the archive:
        _record = self.augment_from_database(event.packet, self.engine.archive)
        # Then augment it with any Ambient-specific data:
        self.augment_protocol(_record)
        # Add the Rapidfire-specific keywords:
        _record['realtime'] = '1'
        _record['rtfreq'] = '2.5'
        # Format and convert to the outgoing protocol
        _request = self.format_protocol(_record)
        # Stuff it in the loop queue:
        self.loop_queue.put((_record['dateTime'], _request))

    def new_archive_record(self, event):
        """Process a new archive event."""
        # Ambient archive posts can just follow the standard protocol model:
        return self.process_record(event.record)

    def augment_protocol(self, record):
        """Augment a record with the Ambient-specific keywords."""
        record['action'] = 'updateraw'
        record['ID'] = self.station
        record['PASSWORD'] = self.password
        record['softwaretype'] = "weewx-%s" % weewx.__version__
        
    def format_protocol(self, record):
        """Given a record, format it using the Ambient protocol.
        
        Performs any necessary unit conversions.
        
        Returns:
        A Request object
        """

        # Requires US units.
        if record['usUnits'] == weewx.US:
            # It's already in US units.
            _datadict = record
        else:
            # It's in something else. Perform the conversion
            _datadict = weewx.units.StdUnitConverters[weewx.US].convertDict(record)
            # Add the new unit system
            _datadict['usUnits'] = weewx.US

        # Reformat according to the Ambient protocol:
        _post_dict = reformat_dict(_datadict, Ambient._formats)

        # Form the full URL
        _url = self.archive_url + '?' + weeutil.weeutil.urlencode(_post_dict)
        # Convert to a Request object:
        _request = urllib2.Request(_url)
        return _request

#===============================================================================
#                    Class StdWunderground
#===============================================================================

class StdWunderground(Ambient):
    """Specialized version of the Ambient protocol for the Weather Underground."""

    # The URLs used by the WU:
    rapidfire_url = "http://rtupdate.wunderground.com/weatherstation/updateweatherstation.php"
    archive_url = "http://weatherstation.wunderground.com/weatherstation/updateweatherstation.php"

    def __init__(self, engine, config_dict):
        
        # First extract the required parameters. If one of them is missing,
        # a KeyError exception will occur. Be prepared to catch it.
        try:
            # Extract the dictionary with the WU options:
            ambient_dict=dict(config_dict['StdRESTful']['Wunderground'])
            ambient_dict.setdefault('rapidfire_url', StdWunderground.rapidfire_url)
            ambient_dict.setdefault('archive_url',   StdWunderground.archive_url)
            ambient_dict.setdefault('name', 'Wunderground')
            super(StdWunderground, self).__init__(engine, config_dict, **ambient_dict)
            syslog.syslog(syslog.LOG_DEBUG, "restx: Data will be posted to Wunderground")
        except ServiceError, e:
            syslog.syslog(syslog.LOG_DEBUG, "restx: Data will not be posted to Wunderground")
            syslog.syslog(syslog.LOG_DEBUG, " ****  Reason: %s" % e)

#===============================================================================
#                    Class StdPWS
#===============================================================================

class StdPWSweather(Ambient):
    """Specialized version of the Ambient protocol for PWS"""

    # The URL used by PWS:
    archive_url = "http://www.pwsweather.com/pwsupdate/pwsupdate.php"

    def __init__(self, engine, config_dict):
        
        try:
            ambient_dict=dict(config_dict['StdRESTful']['PWSweather'])
            ambient_dict.setdefault('archive_url',   StdPWSweather.archive_url)
            ambient_dict.setdefault('name', 'PWSweather')
            super(StdPWSweather, self).__init__(engine, config_dict, **ambient_dict)
            syslog.syslog(syslog.LOG_DEBUG, "restx: Data will be posted to PWSweather")
        except ServiceError, e:
            syslog.syslog(syslog.LOG_DEBUG, "restx: Data will not be posted to PWSweather")
            syslog.syslog(syslog.LOG_DEBUG, " ****  Reason: %s" % e)

#===============================================================================
#                    Class PostRequest
#===============================================================================

class PostRequest(threading.Thread):
    """Post an urllib2 "Request" object, using a separate thread."""
    
    
    def __init__(self, queue, thread_name, **kwargs):
        threading.Thread.__init__(self, name=thread_name)

        self.queue = queue
        self.log_success = to_bool(kwargs.get('log_success', True))
        self.log_failure = to_bool(kwargs.get('log_failure', True))
        self.max_tries   = to_int(kwargs.get('max_tries', 3))
        self.max_backlog = to_int(kwargs.get('max_backlog'))

        self.setDaemon(True)
        
    def run(self):

        while True:

            while True:
                # This will block until a request shows up.
                _request_tuple = self.queue.get()
                # If a "None" value appears in the pipe, it's our signal to exit:
                if _request_tuple is None:
                    return
                # If packets have backed up in the queue, trim it until it's no bigger
                # than the max allowed backlog:
                if self.max_backlog is None or self.queue.qsize() <= self.max_backlog:
                    break

            # Unpack the timestamp and Request object
            _timestamp, _request = _request_tuple

            try:
                # Now post it
                self.post_request(_request)
            except FailedPost:
                if self.log_failure:
                    syslog.syslog(syslog.LOG_ERR, "restx: Failed to upload to '%s'" % self.name)
            except BadLogin, e:
                syslog.syslog(syslog.LOG_CRIT, "restx: Failed to post to '%s'" % self.name)
                syslog.syslog(syslog.LOG_CRIT, " ****  Reason: %s" % e)
                syslog.syslog(syslog.LOG_CRIT, " ****  Terminating %s thread" % self.name)
                return
            else:
                if self.log_success:
                    _time_str = timestamp_to_string(_timestamp)
                    syslog.syslog(syslog.LOG_INFO, "restx: Published record %s to %s" % (_time_str, self.name))

    def post_request(self, request):
        """Post a request.
        
        request: An instance of urllib2.Request
        """

        # Retry up to max_tries times:
        for _count in range(self.max_tries):
            # Now use urllib2 to post the data. Wrap in a try block
            # in case there's a network problem.
            try:
                _response = urllib2.urlopen(request)
            except (urllib2.URLError, socket.error, httplib.BadStatusLine), e:
                # Unsuccessful. Log it and go around again for another try
                syslog.syslog(syslog.LOG_DEBUG, "restx: Failed attempt #%d to upload to %s" % (_count+1, self.name))
                syslog.syslog(syslog.LOG_DEBUG, " ****  Reason: %s" % (e,))
            else:
                # No exception thrown, but we're still not done.
                # We have to also check for a bad station ID or password.
                # It will have the error encoded in the return message:
                for line in _response:
                    # PWSweather signals with 'ERROR', WU with 'INVALID':
                    if line.startswith('ERROR') or line.startswith('INVALID'):
                        # Bad login. No reason to retry. Raise an exception.
                        raise BadLogin, line
                # Does not seem to be an error. We're done.
                return
        else:
            # This is executed only if the loop terminates normally, meaning
            # the upload failed max_tries times. Log it.
            raise FailedPost("Failed upload to site %s after %d tries" % (self.name, self.max_tries))

#===============================================================================
#                             class StdCWOP
#===============================================================================

class StdCWOP(StdRESTbase):
    """Upload using the CWOP protocol. """
    
    # Station IDs must start with one of these:
    valid_prefixes = ['CW', 'DW', 'EW']
    default_servers = ['cwop.aprs.net:14580', 'cwop.aprs.net:23']

    def __init__(self, engine, config_dict):
        
        # First extract the required parameters. If one of them is missing,
        # a KeyError exception will occur. Be prepared to catch it.
        try:
            # Extract the CWOP dictionary:
            cwop_dict=dict(config_dict['StdRESTful']['CWOP'])
            cwop_dict.setdefault('name', 'CWOP')
            cwop_dict['stale']    = to_int(cwop_dict.get('stale', 1800))
            cwop_dict['interval'] = to_int(cwop_dict.get('interval'))
            super(StdCWOP, self).__init__(engine, config_dict, **cwop_dict)

            # Extract the station and (if necessary) passcode
            self.station = cwop_dict['station'].upper()
            if self.station[0:2] in StdCWOP.valid_prefixes:
                self.passcode = "-1"
            else:
                self.passcode = cwop_dict['passcode']
            
        except KeyError, e:
            syslog.syslog(syslog.LOG_DEBUG, "restx: Data will not be posted to CWOP")
            syslog.syslog(syslog.LOG_DEBUG, " ****  Reason: %s" % e)
            return
            
        # If we made it this far, we can post. Everything else is optional.
        self.init_info(cwop_dict)
        
        self.init_archive_queue()
        self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)
        
        # Get the stuff the TNC thread will need....
        cwop_dict.setdefault('max_tries', 3)
        cwop_dict.setdefault('log_success', True)
        cwop_dict.setdefault('log_failure', True)
        if cwop_dict.has_key('server'):
            cwop_dict['server'] = weeutil.weeutil.option_as_list(cwop_dict['server'])
        else:
            cwop_dict['server'] = StdCWOP.default_servers

        # ... launch it ...
        self.archive_thread = PostTNC(self.archive_queue,
                                      cwop_dict['name'],
                                      **cwop_dict)
        # ... then start it
        self.archive_thread.start()

        syslog.syslog(syslog.LOG_DEBUG, "restx: Data will be posted to CWOP station %s" % (self.station,))
        
    def new_archive_record(self, event):
        """Process a new archive event."""
        # CWOP archive posts can just follow the standard protocol model:
        return self.process_record(event.record)

    def get_login_string(self):
        login = "user %s pass %s vers weewx %s\r\n" % (self.station, self.passcode, weewx.__version__)
        return login

    def get_tnc_packet(self, in_record):
        """Form the TNC2 packet used by CWOP."""

        # Make sure the record is in US units:
        if in_record['usUnits'] == weewx.US:
            record = in_record
        else:
            record = weewx.units.StdUnitConverters[weewx.US].convertDict(in_record)
            record['usUnits'] = weewx.US

        # Preamble to the TNC packet:
        prefix = "%s>APRS,TCPIP*:" % (self.station,)

        # Time:
        time_tt = time.gmtime(record['dateTime'])
        time_str = time.strftime("@%d%H%Mz", time_tt)

        # Position:
        lat_str = weeutil.weeutil.latlon_string(self.latitude, ('N', 'S'), 'lat')
        lon_str = weeutil.weeutil.latlon_string(self.longitude, ('E', 'W'), 'lon')
        latlon_str = '%s%s%s/%s%s%s' % (lat_str + lon_str)

        # Wind and temperature
        wt_list = []
        for obs_type in ['windDir', 'windSpeed', 'windGust', 'outTemp']:
            v = record.get(obs_type)
            wt_list.append("%03d" % v if v is not None else '...')
        wt_str = "_%s/%sg%st%s" % tuple(wt_list)

        # Rain
        rain_list = []
        for obs_type in ['hourRain', 'rain24', 'dayRain']:
            v = record.get(obs_type)
            rain_list.append("%03d" % (v * 100.0) if v is not None else '...')
        rain_str = "r%sp%sP%s" % tuple(rain_list)

        # Barometer:
        baro = record.get('altimeter')
        if baro is None:
            baro_str = "b....."
        else:
            # While everything else in the CWOP protocol is in US Customary, they
            # want the barometer in millibars.
            baro_vt = weewx.units.convert((baro, 'inHg', 'group_pressure'), 'mbar')
            baro_str = "b%05d" % (baro_vt[0] * 10.0)

        # Humidity:
        humidity = record.get('outHumidity')
        if humidity is None:
            humid_str = "h.."
        else:
            humid_str = ("h%02d" % humidity) if humidity < 100.0 else "h00"

        # Radiation:
        radiation = record.get('radiation')
        if radiation is None:
            radiation_str = ""
        elif radiation < 1000.0:
            radiation_str = "L%03d" % radiation
        elif radiation < 2000.0:
            radiation_str = "l%03d" % (radiation - 1000)
        else:
            radiation_str = ""

        # Station equipment
        equipment_str = ".weewx-%s-%s" % (weewx.__version__, self.hardware)

        tnc_packet = prefix + time_str + latlon_str + wt_str + rain_str + \
                     baro_str + humid_str + radiation_str + equipment_str + "\r\n"

        return tnc_packet

    def format_protocol(self, record):
        # Get the login string
        _login = self.get_login_string()
        # And the TNC packet
        _tnc_packet = self.get_tnc_packet(record)
        
        return (_login, _tnc_packet)

#===============================================================================
#                    Class PostTNC
#===============================================================================

class PostTNC(threading.Thread):
    """Post using the CWOP TNC protocol."""

    def __init__(self, queue, thread_name, **kwargs):
        threading.Thread.__init__(self, name=thread_name)

        self.queue = queue
        self.log_success = to_bool(kwargs.get('log_success', True))
        self.log_failure = to_bool(kwargs.get('log_failure', True))
        self.max_tries   = to_int(kwargs.get('max_tries', 3))
        self.max_backlog = to_int(kwargs.get('max_backlog'))
        self.server = kwargs['server']

        self.setDaemon(True)

    def run(self):

        while True:
            while True:
                # This will block until a request shows up.
                _request_tuple = self.queue.get()
                # If a "None" value appears in the pipe, it's our signal to exit:
                if _request_tuple is None:
                    return
                # If packets have backed up in the queue, trim it until it's no bigger
                # than the max allowed backlog:
                if self.max_backlog is None or self.queue.qsize() <= self.max_backlog:
                    break

            # Unpack the timestamp, login, tnc packet:
            _timestamp, (_login, _tnc_packet) = _request_tuple

            try:
                # Now post it
                self.send_packet(_login, _tnc_packet)
            except (FailedPost, IOError), e:
                if self.log_failure:
                    syslog.syslog(syslog.LOG_ERR, "restx: Failed to upload to '%s'" % self.name)
                    syslog.syslog(syslog.LOG_ERR, " ****  Reason: %s" % e)
            else:
                if self.log_success:
                    _time_str = timestamp_to_string(_timestamp)
                    syslog.syslog(syslog.LOG_INFO, "restx: Published record %s to %s" % (_time_str, self.name))

    def send_packet(self, _login, _tnc_packet):

        # Get a socket connection:
        _sock = self._get_connect()

        try:
            # Send the login:
            self._send(_sock, _login)

            # And then the packet
            self._send(_sock, _tnc_packet)
        finally:
            _sock.close()

    def _get_connect(self):

        # Go through the list of known server:ports, looking for
        # a connection that works:
        for serv_addr_str in self.server:
            server, port = serv_addr_str.split(":")
            port = int(port)
            for _count in range(self.max_tries):
                try:
                    sock = socket.socket()
                    sock.connect((server, port))
                except socket.error, e:
                    # Unsuccessful. Log it and try again
                    syslog.syslog(syslog.LOG_DEBUG, "restx: Connection attempt #%d failed to %s server %s:%d" % (_count + 1, self.name, server, port))
                    syslog.syslog(syslog.LOG_DEBUG, " ****  Reason: %s" % (e,))
                else:
                    syslog.syslog(syslog.LOG_DEBUG, "restx: Connected to %s server %s:%d" % (self.name, server, port))
                    return sock
                # Couldn't connect on this attempt. Close it, try again.
                try:
                    sock.close()
                except:
                    pass
            # If we got here, that server didn't work. Log it and go on to the next one.
            syslog.syslog(syslog.LOG_DEBUG, "restx: Unable to connect to %s server %s:%d" % (self.name, server, port))

        # If we got here. None of the servers worked. Raise an exception
        raise IOError, "Unable to obtain a socket connection to %s" % (self.name,)

    def _send(self, sock, msg):

        for _count in range(self.max_tries):

            try:
                sock.send(msg)
            except (IOError, socket.error), e:
                # Unsuccessful. Log it and go around again for another try
                syslog.syslog(syslog.LOG_DEBUG, "restx: Attempt #%d failed to send to %s" % (_count + 1, self.name))
                syslog.syslog(syslog.LOG_DEBUG, "  ***  Reason: %s" % (e,))
            else:
                _resp = sock.recv(1024)
                return _resp
        else:
            # This is executed only if the loop terminates normally, meaning
            # the send failed max_tries times. Log it.
            raise FailedPost, "Failed upload to site %s after %d tries" % (self.name, self.max_tries)

#===============================================================================
#                           UTILITIES
#===============================================================================
def reformat_dict(record, format_dict):
    """Given a record, reformat it.
    
    record: A dictionary containing observation types
    
    format_dict: A dictionary containing the key and format to be used for the reformatting.
    The format can either be a string format, or a function.
    
    Example:
    >>> form = {'dateTime'    : ('dateutc', lambda _v : urllib.quote(datetime.datetime.utcfromtimestamp(_v).isoformat('+'), '-+')),
    ...         'barometer'   : ('baromin', '%.3f'),
    ...         'outTemp'     : ('tempf', '%.1f')}
    >>> record = {'dateTime' : 1383755400, 'usUnits' : 16, 'outTemp' : 20.0, 'barometer' : 1020.0}
    >>> print reformat_dict(record, form)
    {'baromin': '1020.000', 'tempf': '20.0', 'dateutc': '2013-11-06+16%3A30%3A00'}
    """

    _post_dict = dict()

    # Go through each of the supported types, formatting it, then adding to _post_dict:
    for _key in format_dict:
        _v = record.get(_key)
        # Check to make sure the type is not null
        if _v is None:
            continue
        # Extract the key to be used in the reformatted dictionary, as well
        # as the format to be used.
        _k, _f = format_dict[_key]
        # First try formatting as a string. If that doesn't work, try it as a function.
        try:
            _post_dict[_k] = _f % _v
        except TypeError:
            _post_dict[_k] = _f(_v)

    return _post_dict

if __name__ == '__main__':
    import doctest

    if not doctest.testmod().failed:
        print "PASSED"
