# -*- coding: utf-8 -*
"""nodoctest
Server the Sage Notebook.
"""

#############################################################################
#       Copyright (C) 2009 William Stein <wstein@gmail.com>
#  Distributed under the terms of the GNU General Public License (GPL)
#  The full text of the GPL is available at:
#                  http://www.gnu.org/licenses/
#############################################################################

# From 5.0 forward, no longer supporting GnuTLS, so only use SSL protocol from OpenSSL
protocol = 'ssl'

# System libraries
import getpass
import os
import shutil
import socket
import sys
import hashlib
from exceptions import SystemExit

from twisted.python.runtime import platformType

from sagenb.misc.misc import (DOT_SAGENB, find_next_available_port,
                              print_open_msg)

import notebook

conf_path     = os.path.join(DOT_SAGENB, 'notebook')

private_pem   = os.path.join(conf_path, 'private.pem')
public_pem    = os.path.join(conf_path, 'public.pem')
template_file = os.path.join(conf_path, 'cert.cfg')

FLASK_NOTEBOOK_CONFIG = """
####################################################################
# WARNING -- Do not edit this file!   It is autogenerated each time
# the notebook(...) command is executed.
####################################################################

import sagenb.notebook.misc
sagenb.notebook.misc.DIR = %(cwd)r #We should really get rid of this!

import signal, sys, random
def save_notebook(notebook):
    print "Quitting all running worksheets..."
    notebook.quit()
    print "Saving notebook..."
    notebook.save()
    print "Notebook cleanly saved."

#########
# Flask #
#########
import os, sys, random
flask_dir = os.path.join(os.environ['SAGE_ROOT'], 'devel', 'sagenb', 'flask_version')
sys.path.append(flask_dir)
import base as flask_base
opts={}
startup_token = '{0:x}'.format(random.randint(0, 2**128))
if %(login)s:
    opts['startup_token'] = startup_token
flask_app = flask_base.create_app(%(notebook_opts)s, **opts)
sys.path.remove(flask_dir)

if %(secure)s:
    from OpenSSL import SSL
    ssl_context = SSL.Context(SSL.SSLv23_METHOD)
    ssl_context.use_privatekey_file(%(pkey)r)
    ssl_context.use_certificate_file(%(cert)r)
else:
    ssl_context = None

import logging
logger=logging.getLogger('werkzeug')
logger.setLevel(logging.WARNING)
#logger.setLevel(logging.INFO) # to see page requests
#logger.setLevel(logging.DEBUG)
logger.addHandler(logging.StreamHandler())

if %(secure)s:
    # Monkey-patch werkzeug so that it works with pyOpenSSL and Python 2.7
    # otherwise, we constantly get TypeError: shutdown() takes exactly 0 arguments (1 given)

    # Monkey patching idiom: http://mail.python.org/pipermail/python-dev/2008-January/076194.html
    def monkeypatch_method(cls):
        def decorator(func):
            setattr(cls, func.__name__, func)
            return func
        return decorator
    from werkzeug import serving

    @monkeypatch_method(serving.BaseWSGIServer)
    def shutdown_request(self, request):
        request.shutdown()

%(open_page)s
try:
    flask_app.run(host=%(host)r, port=%(port)s, threaded=True,
                  ssl_context=ssl_context, debug=False)
finally:
    save_notebook(flask_base.notebook)
"""

TWISTD_NOTEBOOK_CONFIG = """
####################################################################        
# WARNING -- Do not edit this file!   It is autogenerated each time
# the notebook(...) command is executed.
# See http://twistedmatrix.com/documents/current/web/howto/using-twistedweb.html 
#  (Serving WSGI Applications) for the basic ideas of the below code
####################################################################

import sys
if sys.platform.startswith('linux'):
    from twisted.internet import epollreactor
    epollreactor.install()

from twisted.internet import reactor

import sagenb.notebook.misc
sagenb.notebook.misc.DIR = %(cwd)r #We should really get rid of this!

import signal
def save_notebook(notebook):
    from twisted.internet.error import ReactorNotRunning
    print "Quitting all running worksheets..."
    notebook.quit()
    print "Saving notebook..."
    notebook.save()
    print "Notebook cleanly saved."
    
def my_sigint(x, n):
    try:
        reactor.stop()
    except ReactorNotRunning:
        pass
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    
    
signal.signal(signal.SIGINT, my_sigint)

from twisted.web import server

#########
# Flask #
#########
import os, sys, random
flask_dir = os.path.join(os.environ['SAGE_ROOT'], 'devel', 'sagenb', 'flask_version')
sys.path.append(flask_dir)
import base as flask_base
opts={}
startup_token = '{0:x}'.format(random.randint(0, 2**128))
if %(login)s:
    opts['startup_token'] = startup_token
flask_app = flask_base.create_app(%(notebook_opts)s, **opts)
sys.path.remove(flask_dir)

from twisted.web.wsgi import WSGIResource
resource = WSGIResource(reactor, reactor.getThreadPool(), flask_app)

class QuietSite(server.Site):
    def log(*args, **kwargs):
        "Override the logging so that requests are not logged"
        pass

# Log only errors, not every page hit
site = QuietSite(resource)

# To log every single page hit, uncomment the following line
#site = server.Site(resource)

from twisted.application import service, strports
application = service.Application("Sage Notebook")
s = strports.service(%(strport)r, site)
%(open_page)s
s.setServiceParent(application)

#This has to be done after flask_base.create_app is run
from functools import partial
reactor.addSystemEventTrigger('before', 'shutdown', partial(save_notebook, flask_base.notebook))
"""

def cmd_exists(cmd):
    """
    Return True if the given cmd exists.
    """
    return os.system('which %s 2>/dev/null >/dev/null' % cmd) == 0

def get_old_settings(conf):
    """
    Returns three settings from the Twisted configuration file conf:
    the interface, port number, and whether the server is secure.  If
    there are any errors, this returns (None, None, None).
    """
    import re
    # This should match the format written to twistedconf.tac below.
    p = re.compile(r'interface="(.*)",port=(\d*),secure=(True|False)')
    try:
        interface, port, secure = p.search(open(conf, 'r').read()).groups()
        if secure == 'True':
            secure = True
        else:
            secure = False
        return interface, port, secure
    except IOError, AttributeError:
        return None, None, None

def notebook_setup(self=None):
    if not os.path.exists(conf_path):
        os.makedirs(conf_path)

    if not cmd_exists('certtool'):
        raise RuntimeError("You must install certtool to use the secure notebook server.")

    dn = raw_input("Domain name [localhost]: ").strip()
    if dn == '':
        print "Using default localhost"
        dn = 'localhost'

    import random
    template_dict = {'organization': 'SAGE (at %s)' % (dn),
                'unit': '389',
                'locality': None,
                'state': 'Washington',
                'country': 'US',
                'cn': dn,
                'uid': 'sage_user',
                'dn_oid': None,
                'serial': str(random.randint(1, 2 ** 31)),
                'dns_name': None,
                'crl_dist_points': None,
                'ip_address': None,
                'expiration_days': 10000,
                'email': 'sage@sagemath.org',
                'ca': None,
                'tls_www_client': None,
                'tls_www_server': True,
                'signing_key': True,
                'encryption_key': True,
                }
                
    s = ""
    for key, val in template_dict.iteritems():
        if val is None:
            continue
        if val == True:
            w = ''
        elif isinstance(val, list):
            w = ' '.join(['"%s"' % x for x in val])
        else:
            w = '"%s"' % val
        s += '%s = %s \n' % (key, w) 

    f = open(template_file, 'w')
    f.write(s)
    f.close()

    import subprocess

    if os.uname()[0] != 'Darwin' and cmd_exists('openssl'):
        # We use openssl by default if it exists, since it is open
        # *vastly* faster on Linux, for some weird reason.
        cmd = ['openssl genrsa > %s' % private_pem]
        print "Using openssl to generate key"
        print cmd[0]
        subprocess.call(cmd, shell=True)
    else:
        # We checked above that certtool is available.
        cmd = ['certtool --generate-privkey --outfile %s' % private_pem]
        print "Using certtool to generate key"
        print cmd[0]
        subprocess.call(cmd, shell=True)

    cmd = ['certtool --generate-self-signed --template %s --load-privkey %s '
           '--outfile %s' % (template_file, private_pem, public_pem)]
    print cmd[0]
    subprocess.call(cmd, shell=True)

    # Set permissions on private cert
    os.chmod(private_pem, 0600)

    print "Successfully configured notebook."

def notebook_run(self,
             directory     = None,
             port          = 8080,
             interface     = 'localhost',
             port_tries    = 50,
             secure        = False,
             reset         = False,
             accounts      = None,
             openid        = None,

             server_pool   = None,
             ulimit        = '',

             timeout       = 0,

             automatic_login = True,

             start_path    = "",
             fork          = False,
             quiet         = False,

             server = "twistd",
             profile = False,

             subnets = None,
             require_login = None,
             open_viewer = None,
             address = None,
             ):

    if subnets is not None:
        raise ValueError("""The subnets parameter is no longer supported. Please use a firewall to block subnets, or even better, volunteer to write the code to implement subnets again.""")
    if require_login is not None or open_viewer is not None:
        raise ValueError("The require_login and open_viewer parameters are no longer supported.  "
                         "Please use automatic_login=True to automatically log in as admin, "
                         "or use automatic_login=False to not automatically log in.")
    if address is not None:
        raise ValueError("Use 'interface' instead of 'address' when calling notebook(...).")

    cwd = os.getcwd()

    if directory is None:
        directory = '%s/sage_notebook' % DOT_SAGENB
    else:
        if (isinstance(directory, basestring) and len(directory) > 0 and
           directory[-1] == "/"):
            directory = directory[:-1]

    # First change to the directory that contains the notebook directory
    wd = os.path.split(directory)
    if wd[0]:
        os.chdir(wd[0])
    directory = wd[1]

    port = int(port)

    if not secure and interface != 'localhost':
        print '*' * 70
        print "WARNING: Running the notebook insecurely not on localhost is dangerous"
        print "because its possible for people to sniff passwords and gain access to"
        print "your account. Make sure you know what you are doing."
        print '*' * 70

    # first use provided values, if none, use loaded values, 
    # if none use defaults

    nb = notebook.load_notebook(directory)

    directory = nb._dir

    if not quiet:
        print "The notebook files are stored in:", nb._dir

    nb.conf()['idle_timeout'] = int(timeout)

    if openid is not None:
        nb.conf()['openid'] = openid 
    elif not nb.conf()['openid']:
        # What is the purpose behind this elif?  It seems rather pointless.
        # all it appears to do is set the config to False if bool(config) is False
        nb.conf()['openid'] = False

    if accounts is not None:
        nb.user_manager().set_accounts(accounts)
    else:
        nb.user_manager().set_accounts(nb.conf()['accounts'])

    if nb.user_manager().user_exists('root') and not nb.user_manager().user_exists('admin'):
        # This is here only for backward compatibility with one
        # version of the notebook.
        s = nb.create_user_with_same_password('admin', 'root')
        # It would be a security risk to leave an escalated account around.

    if not nb.user_manager().user_exists('admin'):
        reset = True

    if reset:
        passwd = get_admin_passwd()                
        if reset:
            admin = nb.user_manager().user('admin')
            admin.set_password(passwd)
            print "Password changed for user 'admin'."
        else:
            nb.user_manager().create_default_users(passwd)
            print "User admin created with the password you specified."
            print "\n\n"
            print "*" * 70
            print "\n"
            if secure:
                print "Login to the Sage notebook as admin with the password you specified above."
        #nb.del_user('root')
            
    nb.set_server_pool(server_pool)
    nb.set_ulimit(ulimit)
    
    if os.path.exists('%s/nb-older-backup.sobj' % directory):
        nb._migrate_worksheets()
        os.unlink('%s/nb-older-backup.sobj' % directory)
        print "Updating to new format complete."


    nb.upgrade_model()

    nb.save()
    del nb

    def run_flask():
        """Run a flask (werkzeug) webserver."""
        # TODO: Check to see if server is running already (PID file?)
        conf = os.path.join(directory, 'run_flask')

        notebook_opts = '"%s",interface="%s",port=%s,secure=%s' % (
            os.path.abspath(directory), interface, port, secure)

        if automatic_login:
            start_path = "'/?startup_token=%s' % startup_token"
            if interface:
                hostname = interface
            else:
                hostname = 'localhost'
            open_page = "from sagenb.misc.misc import open_page; open_page('%s', %s, %s, %s)" % (hostname, port, secure, start_path)
        else:
            open_page = ''

        config = open(conf, 'w')

        config.write(FLASK_NOTEBOOK_CONFIG%{'notebook_opts': notebook_opts,
                                            'cwd':cwd,
                                            'open_page': open_page, 'login': automatic_login,
                                            'secure': secure, 'pkey': private_pem, 'cert': public_pem,
                                            'host': interface, 'port': port})

        config.close()

        if profile:
            import random
            if isinstance(profile, basestring):
                profilefile = profile+'%s.stats'%random.random()
            else:
                profilefile = 'sagenb-flask-profile-%s.stats'%random.random()
            profilecmd = '-m cProfile -o %s'%profilefile
        else:
            profilecmd=''
        cmd = 'python %s %s' % (profilecmd, conf)
        return cmd
        # end of inner function run_flask

    def run_twistd():
        """Run a twistd webserver."""
        # Is a server already running? Check if a Twistd PID exists in
        # the given directory.
        conf = os.path.join(directory, 'twistedconf.tac')
        pidfile = os.path.join(directory, 'twistd.pid')
        if platformType != 'win32':
            from twisted.scripts._twistd_unix import checkPID
            try:
                checkPID(pidfile)
            except SystemExit as e:
                pid = int(open(pidfile).read())

                if str(e).startswith('Another twistd server is running,'):
                    print 'Another Sage Notebook server is running, PID %d.' % pid

                    old_interface, old_port, old_secure = get_old_settings(conf)
                    if automatic_login and old_port:
                        old_interface = old_interface or 'localhost'

                        print 'Opening web browser at http%s://%s:%s/ ...' % (
                            's' if old_secure else '', old_interface, old_port)

                        from sagenb.misc.misc import open_page as browse_to
                        browse_to(old_interface, old_port, old_secure, '/')
                        return None
                    print '\nPlease either stop the old server or run the new server in a different directory.'
                    return None

        ## Create the config file
        if secure:
            strport = '%s:%s:interface=%s:privateKey=%s:certKey=%s'%(
                protocol, port, interface, private_pem, public_pem)
        else:
            strport = 'tcp:%s:interface=%s' % (port, interface)

        notebook_opts = '"%s",interface="%s",port=%s,secure=%s' % (
            os.path.abspath(directory), interface, port, secure)

        if automatic_login:
            start_path = "'/?startup_token=%s' % startup_token"
            if interface:
                hostname = interface
            else:
                hostname = 'localhost'
            open_page = "from sagenb.misc.misc import open_page; open_page('%s', %s, %s, %s)" % (hostname, port, secure, start_path)
        else:
            open_page = ''

        config = open(conf, 'w')

        config.write(TWISTD_NOTEBOOK_CONFIG%{'notebook_opts': notebook_opts,
                                            'cwd':cwd, 'strport': strport,
                                            'open_page': open_page, 'login': automatic_login})


        config.close()

        ## Start up twisted
        if profile:
            import random
            if isinstance(profile, basestring):
                profilefile = profile+'%s.stats'%random.random()
            else:
                profilefile = 'sagenb-twistd-profile-%s.stats'%random.random()
            profilecmd = '--profile=%s --profiler=cprofile --savestats'%profilefile
        else:
            profilecmd=''
        cmd = 'twistd %s --pidfile="%s" -ny "%s"' % (profilecmd, pidfile, conf)
        return cmd
        # end of inner function run_twistd

    if interface != 'localhost' and not secure:
            print "*" * 70
            print "WARNING: Insecure notebook server listening on external interface."
            print "Unless you are running this via ssh port forwarding, you are"
            print "**crazy**!  You should run the notebook with the option secure=True."
            print "*" * 70

    port = find_next_available_port(interface, port, port_tries)
    if automatic_login:
        "Automatic login isn't fully implemented.  You have to manually open your web browser to the above URL."
    if secure:
        if (not os.path.exists(private_pem) or
            not os.path.exists(public_pem)):
            print "In order to use an SECURE encrypted notebook, you must first run notebook.setup()."
            print "Now running notebook.setup()"
            notebook_setup()
        if (not os.path.exists(private_pem) or
            not os.path.exists(public_pem)):
            print "Failed to setup notebook.  Please try notebook.setup() again manually."
    if server=="flask":
        cmd = run_flask()
    elif server=="twistd":
        cmd = run_twistd()
    if cmd is None:
        return
    
    if not quiet:
        print_open_msg('localhost' if not interface else interface,
        port, secure=secure)
    if secure and not quiet:
        print "There is an admin account.  If you do not remember the password,"
        print "quit the notebook and type notebook(reset=True)."
    print "Executing", cmd
    if fork:
        import pexpect
        return pexpect.spawn(cmd)
    else:
        e = os.system(cmd)

    os.chdir(cwd)
    if e == 256:
        raise socket.error

def get_admin_passwd():
    print "\n" * 2
    print "Please choose a new password for the Sage Notebook 'admin' user."
    print "Do _not_ choose a stupid password, since anybody who could guess your password"
    print "and connect to your machine could access or delete your files."
    print "NOTE: Only the hash of the password you type is stored by Sage."
    print "You can change your password by typing notebook(reset=True)."
    print "\n" * 2
    while True:
        passwd = getpass.getpass("Enter new password: ")
        from sagenb.misc.misc import min_password_length
        if len(passwd) < min_password_length:
            print "That password is way too short. Enter a password with at least %d characters."%min_password_length
            continue
        passwd2 = getpass.getpass("Retype new password: ")
        if passwd != passwd2:
            print "Sorry, passwords do not match."
        else:
            break

    print "Please login to the notebook with the username 'admin' and the above password."
    return passwd
