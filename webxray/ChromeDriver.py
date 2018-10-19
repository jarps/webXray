# standard python packages
import os
import re
import json
import time
import random
import sqlite3

# check if non-standard packages are installed
try:
    from seleniumwire import webdriver
    from selenium.webdriver.chrome.options import Options
except:
    print('*****************************************************************')
    print(' FATAL ERROR: Selenium is not installed, required to use Chrome! ')
    print('*****************************************************************')
    exit()

from webxray.Utilities import Utilities

class ChromeDriver:
    """
    This class allows for using the production Chrome browser with webXray.
    Requirements are Selenium, Chrome, and ChromeDriver.

    Pros:
        Production browser which is largely identical to real-world use
        By turning headless off it is very similar to a 'real' session
        By turning headless on the CPU/Mem usage is lower than otherwise
    Cons:
        Less testing with webxray than phantomjs, does not handle many paralell instances very well
        In headless mode prior to 64.0.3254.0, the cookie database does not get created and no cookies are returned
    """

    def __init__(self,ua=False, dnt=False):
        """
        set various global options here
        """

        self.dnt = dnt
        
        # set here if you want to use headless mode for Chrome
        self.headless = True

        # if you want to get potentially dangerous requests, set 
        #	this to true.
        # false by default for hopefully obvious reasons
        self.allow_insecure = False

        # if you have trouble getting chrome to start
        #	change these values manually
        self.chromedriver_path  = None
        self.chrome_binary_path = None

        # we want to give our browsers a full minute to try to
        #	download content, but gracefully timeout thereafter
        self.page_timeout_seconds = 60

        # Set ua if we have it, see get_ua_for_headless
        #	for details
        self.ua = ua
        
        # useful for various tasks
        self.utilities = Utilities()

        return None
    # init

    def create_chromedriver(self):
        """
        Since we have many functions we can perform we consolidate
            chromedriver code here.
        """

        # set up options object
        chrome_options = Options()

        # if we have chrome binary set it up
        if self.chrome_binary_path:
            chrome_options.binary_location = self.chrome_binary_path
        
        # live dangerously
        if self.allow_insecure:
            chrome_options.add_argument('--allow-running-insecure-content')

        # thank god for this option
        chrome_options.add_argument('--mute-audio')
        
        # if we are headless we also mix up window size a bit
        if self.headless:
            chrome_options.add_argument('headless')
            chrome_options.add_argument('disable-gpu')
            window_x = random.randrange(1050,1920)
            window_y = random.randrange(900,1080)
            chrome_options.add_argument('window-size=%sx%s' % (window_x,window_y))

        # if we have a ua set it here
        if self.ua: 
            chrome_options.add_argument('user-agent='+self.ua)

        # attempt to start driver, fail gracefull otherwise
        try:
            # if we have chromedriver path set it up
            if self.chromedriver_path:
                driver = webdriver.Chrome(
                    self.chromedriver_path,
                    desired_capabilities={'loggingPrefs': {'performance': 'ALL'}},
                    chrome_options=chrome_options
                )
            else:
                driver = webdriver.Chrome(
                    desired_capabilities={'loggingPrefs': {'performance': 'ALL'}},
                    chrome_options=chrome_options,      
                )
        except:
            return None

        # allow one minute before we kill it, seperate from browser_wait
        driver.set_page_load_timeout(self.page_timeout_seconds)

        return driver
    # init_headless_driver

    def get_ua_for_headless(self):
        """
        Using chrome in headless sends a 'Headless' ua string,
            here we figure out the current ua and remove the 
            'Headless' part to help with compatability

        This requires firing up a new browser instance
            and destroying it, so this should be called once
            and resused if possible and this is not in
            __init___ on purpose
        """
        driver = self.create_chromedriver()
        if driver != None:
            ua = driver.execute_script('return navigator.userAgent')
            driver.quit()
            return ua.replace('Headless','')
        else:
            return None
    # get_ua_for_headless

    def get_webxray_scan_data(self, url, browser_wait):
        """
        This function loads the page, monitors network traffic, and returns relevant data/logs.

        IMPORTANT: headless will miss all cookies in chrome versions < 64.0.3254.0

        This uses the chrome performance log to get network traffic details, see following for details:
            - https://gist.githubusercontent.com/klepikov/5457750/raw/ecedc6dd4eed82f318db91adb923627716fb6b58/test.py
            - https://sites.google.com/a/chromium.org/chromedriver/logging/performance-log
        """

        driver = self.create_chromedriver()
        # we can't start Chrome, return error message as result
        if driver == None:
            return({
                'success': False,
                'result': 'Unable to launch Chrome instance'
            })

        # allow one minute before we kill it, seperate from browser_wait
        driver.set_page_load_timeout(60)

        # start the page load process, return error message if we fail
        try:
            if self.dnt:
                print("DNT on")
                driver._client.set_header_overrides(headers = {'DNT': 1})
            driver.get(url)
        except:
            driver.quit()
            return({
                'success': False,
                'result': 'Unable to load page'
            })

        # if the page has an alert window open it will throw the following exception when trying
        #	to get the current_url: selenium.common.exceptions.UnexpectedAlertPresentException
        # in theory we should be able to set an option for UNEXPECTED_ALERT_BEHAVIOUR to ACCEPT
        # 	but it does not seem to be supported by chromedriver at present
        # in some cases we can catch the items we need before an alert fires, otherwise
        # 	we fail gracefully, but this is a bug that needs resolution
        try:
            final_url 	= driver.current_url
            title 		= driver.title
            page_source = driver.page_source
        except:
            # quit the driver or it will never die!
            driver.quit()
            return({
                'success': False,
                'result': 'Unable to load page, possible javascript alert issue'
            })

        # handle odd bug where title is a 'webelement' object
        if not isinstance(title, str): title = None

        # while the browser may be finished loading the page, scripts may still making
        # 	additional requests, so we wait to let all that finish
        time.sleep(browser_wait)

        # We use the Chrome performance log get network traffic. Chrome performance log outputs a 
        #	number of independent 'message' events which are keyed to a 'requestId'.  What we want
        #	to send upstream is a dictionary keyed on the requested url so we do a lot of processing
        #	here to stitch together a coherent log in the format expected by wbxr.
        #
        # There are two types of network events we are concerned with: normal http 
        #	requests (initiated by Network.requestWillBeSent) and websocket requests (initiated
        #	by Network.webSocketCreated).
        #
        # For normal events, we add entries to the 'requests' dictionary which we key to the requested
        #	url.  The reason for this is a single requestId may correspond with many urls in
        #	cases where a request results in redirects occuring.  However, data from the 
        #	Network.loadingFinished event does not include the url, so we key that seperately
        #	in the load_finish_data dict and then attach it later on.  Note that if a request to
        #	x.com results in redirects to y.com and z.com, all three will end up sharing
        #	the same loadingFinished data.
        #
        # webSocket events are a special case in that they are not strictly HTTP events, but 
        #	they do two things we are concerned with: potentially linking a user to 
        #	a third-party domain and setting cookies.  The url contacted is only exposed in the
        #	first event, Network.webSocketCreated, so we must use the requestId to tie together
        #	subsequent Network.webSocketWillSendHandshakeRequest and 
        #	Network.webSocketHandshakeResponseReceived events.  We use the dictionary websocket_requests
        #	to keep track of such events, and we then reprocess them to be keyed to the url in our
        #	normal requests log.  Note that to keep track of websocket request we use 'websocket'
        #	for content type, and there may be a better way to handle this.

        # http requests are keyed to URL
        requests 		   = {}
        
        # these events are keyed to requestID
        load_finish_data   = {}
        websocket_requests = {}

        # to get page load time we will figure out when the first request and final load finished occured
        first_start_time = None
        last_end_time 	 = None

        # for debuging
        duplicate_keys = []

        # crunch through all the chrome logs here, the main event!
        for log_item in driver.get_log('performance'):
            for key, this_log_item in log_item.items():
                # we are only interested in message events
                if key == 'message':
                    # we have to read in this value to get json data
                    log_item_data 	= json.loads(this_log_item)
                    message_type	= log_item_data['message']['method']

                    ################################
                    # normal http event processing #
                    ################################

                    # we have a new http event, create new empty entry keyed to url
                    # and keep track of start time info
                    if message_type == 'Network.requestWillBeSent':
                        this_request = log_item_data['message']['params']['request']
                        this_url 	 = this_request['url']
                        
                        # skip if not http(s)
                        if not re.match('^https?://', this_url): continue

                        # if a new request we initialize entry
                        if this_url not in requests:
                            requests[this_url] = {}

                            # we use this to get the load_finish_data later on
                            requests[this_url].update({'request_id': log_item_data['message']['params']['requestId']})
    
                            # we set received to false to start with
                            requests[this_url].update({'received':			False})

                            # initialze response values to None in case we don't get response
                            requests[this_url].update({'end_time':			None})
                            requests[this_url].update({'status':			None})
                            requests[this_url].update({'status_text':		None})
                            requests[this_url].update({'response_headers':	None})
                            requests[this_url].update({'content_type':		None})
                            requests[this_url].update({'body_size':			None})
                            requests[this_url].update({'end_time':			None})
                            requests[this_url].update({'user_agent':		None})
                            requests[this_url].update({'referer':			None})

                            # each request has a start_time, we use this to figure out the time it took to download
                            this_start_time = log_item_data['message']['params']['timestamp']
                            requests[this_url].update({'start_time':this_start_time})
                            
                            # update global start time to measure page load time
                            if first_start_time == None or this_start_time < first_start_time:
                                first_start_time = this_start_time

                            # get the request headers
                            requests[this_url].update({'request_headers':this_request['headers']})

                            # these can fail, if so, we ignore
                            try:
                                requests[this_url].update({'user_agent':this_request['headers']['User-Agent']})
                            except:
                                pass

                            try:
                                requests[this_url].update({'referer':this_request['headers']['Referer']})
                            except:
                                pass
                        # this_url already exists, log
                        else:
                            duplicate_keys.append(this_url)
                            continue

                    # we have received a response to our request, update appropriately
                    if message_type == 'Network.responseReceived':
                        this_response 	= log_item_data['message']['params']['response']
                        this_url 	 	= this_response['url']

                        # skip if not http(s)
                        if not re.match('^https?://', this_url): continue

                        # the request was received, mark it
                        requests[this_url].update({'received':		True})

                        # record status code and text
                        requests[this_url].update({'status':		this_response['status']})
                        requests[this_url].update({'status_text':	this_response['statusText']})
                        
                        # try to get response headers, fail gracefully as they are already None
                        try:
                            requests[this_url].update({'response_headers':this_response['headersText']})
                        except:
                            pass
                        
                        try:
                            requests[this_url].update({'content_type':this_response['headers']['Content-Type']})
                        except:
                            pass

                    # load finish events are keyed to requestId and may apply to many requested urls
                    #	so we keep this in a seperate dictionary to be relinked when we're done
                    if message_type == 'Network.loadingFinished':
                        this_request_id = log_item_data['message']['params']['requestId']
                        this_end_time	= log_item_data['message']['params']['timestamp']

                        # update global end time
                        if last_end_time == None or this_end_time > last_end_time:
                            last_end_time = this_end_time

                        if this_request_id not in load_finish_data:
                            load_finish_data[this_request_id] = {}

                        # size is updated during loading and is shown in logs, but we only want the final size which is here
                        load_finish_data[this_request_id].update({'body_size':log_item_data['message']['params']['encodedDataLength']})

                        # we use this to calculate the total time for all requests
                        load_finish_data[this_request_id].update({'end_time':this_end_time})

                    ##############################
                    # webSocket event processing #
                    ##############################

                    # we have a new websocket, create new empty entry keyed to requestId
                    # 	this will be rekeyed to url
                    # note we ignore timing data for websockets
                    if message_type == 'Network.webSocketCreated':
                        this_url 		= log_item_data['message']['params']['url']
                        this_request_id = log_item_data['message']['params']['requestId']

                        if this_request_id not in websocket_requests:
                            websocket_requests[this_request_id] = {}
                            websocket_requests[this_request_id].update({'url': 				this_url})
                            websocket_requests[this_request_id].update({'content_type':		'websocket'})
                            websocket_requests[this_request_id].update({'received':			False})
                            websocket_requests[this_request_id].update({'end_time':			None})
                            websocket_requests[this_request_id].update({'status':			None})
                            websocket_requests[this_request_id].update({'status_text':		None})
                            websocket_requests[this_request_id].update({'response_headers':	None})
                            websocket_requests[this_request_id].update({'body_size':		None})
                            websocket_requests[this_request_id].update({'end_time':			None})
                            websocket_requests[this_request_id].update({'start_time':		None})
                            websocket_requests[this_request_id].update({'user_agent':		None})
                            websocket_requests[this_request_id].update({'referer':			None})

                    # websocket request made, update relevant fields
                    if message_type == 'Network.webSocketWillSendHandshakeRequest':
                        this_request 	= log_item_data['message']['params']['request']
                        this_request_id = log_item_data['message']['params']['requestId']
                        websocket_requests[this_request_id].update({'request_headers':	this_request['headers']})
                        websocket_requests[this_request_id].update({'user_agent':		this_request['headers']['User-Agent']})

                    # websocket response received, update relevant fields
                    if message_type == 'Network.webSocketHandshakeResponseReceived':
                        this_response 	= log_item_data['message']['params']['response']
                        this_request_id = log_item_data['message']['params']['requestId']
                        websocket_requests[this_request_id].update({'received':			True})
                        websocket_requests[this_request_id].update({'status':			this_response['status']})
                        websocket_requests[this_request_id].update({'status_text':		this_response['statusText']})
                        websocket_requests[this_request_id].update({'response_headers':	this_response['headersText']})
        # end log processing loop

        # append load finish info to requests
        for this_url in requests:
            this_request_id = requests[this_url]['request_id']
            if this_request_id in load_finish_data:
                requests[this_url].update({'body_size': load_finish_data[this_request_id]['body_size']})
                
                # load_time is start time minus end time,
                # 	multiplied by 1k to convert to miliseconds
                load_time = (load_finish_data[this_request_id]['end_time'] - requests[this_url]['start_time'])*1000
                
                # we shouldn't be getting <=0, but make it null if this happens
                if load_time <= 0:
                    requests[this_url].update({'load_time': load_time})
                else:
                    requests[this_url].update({'load_time': None})
            else:
                requests[this_url].update({'body_size': None})
                requests[this_url].update({'load_time': None})

        # append websocket data to requests data
        for item in websocket_requests:
            requests[websocket_requests[item]['url']] = websocket_requests[item]

        # return all the links for later processing
        all_links = []
        try:
            links = driver.find_elements_by_tag_name('a')
            for link in links:
                all_links.append([link.get_attribute('text'),link.get_attribute('href')])
        except:
            pass

        # get the page meta description
        try:
            meta_desc = driver.find_element_by_xpath("//meta[@name='description']").get_attribute("content")
        except:
            meta_desc = None

        # get the language of the page
        try:
            lang = driver.find_element_by_xpath('/html').get_attribute('lang')
        except:
            lang = None

        # get all the cookies
        # 	the selenium get_cookies method does not return third-party cookies
        #	so we open the cookie db directly from the chrome profile
        #	note that in headless mode this does not work in chrome versions
        #	prior to 64.0.3254.0 and no cookies will be returned
        cookies = []
        try:
            conn = sqlite3.connect(driver.capabilities['chrome']['userDataDir']+'/Default/Cookies')
            c = conn.cursor()
            c.execute("SELECT name,is_secure,path,host_key,expires_utc,is_httponly,value FROM cookies")
            for cookie in c.fetchall():
                cookies.append({
                    'name': 		cookie[0],
                    'secure':		cookie[1],
                    'path':			cookie[2],
                    'domain': 		cookie[3],
                    'expiry':		cookie[4],
                    'httponly':		cookie[5],
                    'value':		cookie[6]
                })
        except:
            return({
                'success': False,
                'result': 'Cookie database not loaded, if this message appears often something is fundamentally wrong and requires attention!'
            })

        if self.headless == True:
            browser_version = driver.capabilities['version'] + ' [headless]'
        else:
            browser_version = driver.capabilities['version']

        # other parts of webxray expect this data format, common to all browser drivers used
        return_dict = {
            'browser_type':			driver.capabilities['browserName'],
            'browser_version':		browser_version,
            'browser_wait':			browser_wait,
            'start_url':			url, 
            'final_url': 			final_url,
            'title': 				title,
            'meta_desc': 			meta_desc,
            'lang':					lang,
            'load_time': 			int((last_end_time - first_start_time)*1000),
            'processed_requests': 	requests,
            'cookies': 				cookies,
            'all_links':			all_links,
            'source':				page_source
        }
        
        # quit the driver or it will never die!
        driver.quit()

        return ({
            'success': True,
            'result': return_dict
        })
    # get_webxray_scan_data

    def get_page_source_text_title_lang(self, url):
        """
        Given a url, this function attempts to load the page, inject
            the Readability.js library, execute it, and extract
            only the text of a given page.

        Function returns the success value (True/False), page source, the 
            extracted text, and the page title in case of success; in 
            case of failure returns an error message.
        """

        # set up sql_driver for logging errors
        driver = self.create_chromedriver()

        # browser hasn't started and error already printed to cli
        if driver == None:
            return({
                'success': False,
                'result': 'Unable to launch Chrome instance'
            })

        # starts the page load process, quits driver and returns nothing if we fail
        try:
            driver.get(url)
        except:
            driver.quit()
            return({
                'success': False,
                'result': 'Unable to load page'
            })

        # if we can't get source something is wrong, return None
        try:
            page_source = driver.page_source
        except:
            driver.quit()
            return({
                'success': False,
                'result': 'Unable to read page source'
            })

        # if page title fails put in a null value
        try:
            page_title = driver.title
        except:
            page_title = None

        # get the language of the page
        try:
            page_lang = driver.find_element_by_xpath('/html').get_attribute('lang')
        except:
            page_lang = None

        # inject the locally downloaded copy of readability into the page
        #	and extract the content
        #
        # NOTE: you must download readability on your own and place in the 
        #	appropriate directory
        readability_js = open(os.path.dirname(os.path.abspath(__file__))+'/resources/policyxray/readability.js', 'r', encoding='utf-8').read()
        try:
            readabilty_result = json.loads(driver.execute_script("""
                %s
                var readabilityObj = new Readability("%s", document);
                return JSON.stringify(readabilityObj.parse(), null, 2);
            """ % (readability_js,url)))
            driver.quit()
        except:
            driver.quit()
            return({
                'success': False,
                'result': 'Unable to inject Readability.js'
            })

        # readability failure, return None
        if readabilty_result == None:
            return({
                'success': False,
                'result': 'Empty Readability result'
            })

        # readability has HTML formatting, remove it (and extra spaces), return None on failure
        try:
            page_text = re.sub('\s+', ' ', re.sub('&.+?;', ' ', re.sub('<.+?>', ' ', readabilty_result['content'])))
        except:
            return({
                'success': False,
                'result': 'Unable to remove HTML from Readability result'
            })

        # the page text is empty, return None
        if len(page_text) == 0: 
            return({
                'success': False,
                'result': 'Empty result after HTML removal'
            })

        # looks good, return tuple
        return({
            'success': True,
            'result': {
                'page_source': 	page_source,
                'page_text': 	page_text,
                'page_title': 	page_title,
                'page_lang': 	page_lang
            }
        })
    # get_page_source_text_title_lang

    def get_absolute_page_links(self,url):
        """
        Returns all links on the page, changes relative links to
            absolute links as well.
        """

        # initialize browser instance
        driver = self.create_chromedriver()

        # browser hasn't started and error already printed to cli
        if driver == None:
            return({
                'success': False,
                'result': 'Unable to launch Chrome instance'
            })

        # allow one minute before we kill it
        driver.set_page_load_timeout(60)

        # starts the page load process, quits driver and returns nothing if we fail
        try:
            driver.get(url)
        except:
            driver.quit()
            return({
                'success': False,
                'result': 'Unable to load page'
            })

        # page has now been loaded, we process all the links
        all_links = set()
        
        # either there are no links or something has gone wrong
        try:
            links = driver.find_elements_by_tag_name('a')
        except:
            driver.quit()
            return({
                'success': False,
                'result': 'Unable to extract links'
            })

        # process the links
        for link in links:
            # to cope with:
            # selenium.common.exceptions.StaleElementReferenceException: Message: stale element reference: element is not attached to the page document
            try:
                this_link = link.get_attribute('href')
                this_link_text = re.sub('\s+', ' ', re.sub('[\\n|\\r|\\t]',' ',link.get_attribute('text').strip()))
            except:
                continue

            # sometimes can be empty
            if this_link == None: continue
            if len(this_link) == 0: continue

            # add in the absolute url from the link to our set
            all_links.add((this_link_text,self.utilities.get_absolute_url_from_page_link(url,this_link)))
        driver.quit()

        return({
            'success': True,
            'result': all_links
        })
    # get_absolute_page_links
# ChromeDriver
