#!/bin/env python3

import sys
import os
import re
import subprocess
import signal
import json
import logging
import ssl
import OpenSSL
import shlex
from datetime import datetime
from pathlib import Path
from enum import Enum
from urllib.parse import urlparse
import asyncio
import websockets
# import websockets.speedups
from XRootD import client

if sys.version_info[0] != 3 or sys.version_info[1] < 6:
    print("This script requires a minimum of Python version 3.6")
    sys.exit(1)


# xrdcp generic parameters (used by ALICE tests)
FirstConnectMaxCnt = 2
TransactionTimeout = 60
RequestTimeout = 60
ReadCacheSize = 0
xrdcp_args = f"&FirstConnectMaxCnt={FirstConnectMaxCnt}&TransactionTimeout={TransactionTimeout}&RequestTimeout={RequestTimeout}&ReadCacheSize={ReadCacheSize}"

# XRootD copy parameters
# inittimeout: copy initialization timeout(int)
# tpctimeout: timeout for a third-party copy to finish(int)
# coerce: ignore file usage rules, i.e. apply `FORCE` flag to open() (bool)
#:param checksummode: checksum mode to be used #:type    checksummode: string
#:param checksumtype: type of the checksum to be computed  #:type    checksumtype: string
#:param checksumpreset: pre-set checksum instead of computing it #:type  checksumpreset: string
hashtype = str('md5')
sources = int(1)  # max number of download sources
chunks = int(1)  # number of chunks that should be requested in parallel
chunksize = int(4194304)  # chunk size for remote transfers
makedir = bool(True)  # create the parent directories when creating a file
overwrite = bool(True)  # overwrite target if it exists
posc = bool(True)  # persist on successful close; Files are automatically deleted should they not be successfully closed.

# environment debug variable
DEBUG = os.getenv('JALIENPY_DEBUG', '')
XRDDEBUG = os.getenv('JALIENPY_XRDDEBUG', '')

# Steering output
json_output = bool(False)
json_meta_output = bool(False)

# other user oriented variables
homedir = os.getenv('HOME', '~')
tmpdir = os.getenv('TMPDIR', '/tmp')
fUser = os.getenv('alien_API_USER', os.getenv('LOGNAME', 'USER'))

# let get the token file name
UID = os.getuid()

# SSL SETTINGS
cert = None
key = None
capath_default = os.getenv('X509_CERT_DIR', '/etc/grid-security/certificates')

# user cert locations
userproxy = '/tmp' + '/x509up_u' + str(UID)

user_globus_dir = homedir + '/.globus'
usercert_default = user_globus_dir + '/usercert.pem'
userkey_default = user_globus_dir + '/userkey.pem'

usercert = os.getenv('X509_USER_CERT', usercert_default)
userkey = os.getenv('X509_USER_KEY', userkey_default)

# token certificate
getToken = bool(False)
tokencert_default = tmpdir + "/tokencert_" + str(UID) + ".pem"
tokenkey_default = tmpdir + "/tokenkey_" + str(UID) + ".pem"
tokencert = os.getenv('JALIEN_TOKEN_CERT', tokencert_default)
tokenkey = os.getenv('JALIEN_TOKEN_KEY', tokenkey_default)

# Web socket static variables
websocket = None  # global websocket
fWSPort = 8097  # websocket port
fHostWS = ''
fHostWSUrl = ''

# Websocket endpoint to be used
server_central = 'alice-jcentral.cern.ch'
ws_path = '/websocket/json'
default_server = server_central

# jalien_py internal vars
fGridHome = str('')
currentdir = ''
commandlist = ''
site = ''
user = ''
error = ''
exitcode = ''

# current command in execution
ccmd = ''

# command history
cmd_hist = []


class MyCopyProgressHandler(client.utils.CopyProgressHandler):
    isUpload = False
    src = ''  # pass the source from begin to end
    dst = ''  # pass the target from begin to end
    token_list_upload_ok = []  # record the tokens of succesfully uploaded files. needed for commit to catalogue

    def begin(self, id, total, source, target):
        print("jobID: {0}/{1}".format(id, total))
        self.src = source
        self.dst = target
        if XRDDEBUG:
            print("source: {}".format(source))
            print("target: {}".format(target))

    def end(self, jobId, results):
        results_message = results['status'].message
        results_status = results['status'].status
        results_errno = results['status'].errno
        results_code = results['status'].code
        status = ''
        if results['status'].ok: status = 'OK'
        if results['status'].error: status = 'ERROR'
        if results['status'].fatal: status = 'FATAL'

        if results['status'].ok:
            print("STATUS: {0} ; MESSAGE: {1}".format(status, results_message))
        else:
            print("STATUS: {0} ; ERRNO: {1} ; CODE: {2} ; MESSAGE: {3}".format(results_status, results_errno, results_code, results_message))

        if not self.isUpload:
            if results['status'].ok:
                os.remove(urlparse(str(self.src)).path)

        if self.isUpload:
            if results['status'].ok:
                dst_str = str(self.dst)
                link = urlparse(dst_str)
                token = next((param for param in str.split(link.query, '&') if 'authz=' in param), None).replace('authz=', '')  # extract the token from url
                self.token_list_upload_ok.append(str(token))

    def update(self, jobId, processed, total):
        print("jobID : {0} ; processed: {1}, total: {2}".format(jobId, processed, total))


def XrdCopy(src, dst):
    global websocket, overwrite, sources, chunks, chunksize, makedir, posc, hashtype
    process = client.CopyProcess()
    handler = MyCopyProgressHandler()
    handler.wb = websocket
    if len(dst) > 1: handler.isUpload = True  # this is a upload job because there are multiple destinations
    for url_src in src:
        for url_dst in dst:
            process.add_job(url_src["url"], url_dst["url"], force = overwrite, posc = posc, mkdir = makedir, chunksize = chunksize, parallelchunks = chunks,
                            sourcelimit = sources, checksumtype = hashtype)
    process.prepare()
    process.run(handler)
    return handler.token_list_upload_ok  # for upload jobs we must return the list of token for succesful uploads


def md5(file):
    import hashlib
    BLOCKSIZE = 65536
    hasher = hashlib.md5()
    with open(file, 'rb') as f:
        buf = f.read(BLOCKSIZE)
        while len(buf) > 0:
            hasher.update(buf)
            buf = f.read(BLOCKSIZE)
    return hasher.hexdigest()


def signal_handler(sig, frame):
    print('\nExit')
    sys.exit(0)


def exit_message():
    print('\nExit')
    sys.exit(0)


def IsValidCert(fname):
    try:
        with open(fname) as f:
            cert_bytes = f.read()
    except Exception:
        return False

    try:
        x509 = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM, cert_bytes)
    except Exception:
        return False

    x509_notafter = x509.get_notAfter()
    utc_time = datetime.strptime(x509_notafter.decode("utf-8"), "%Y%m%d%H%M%SZ")
    time_notafter = int((utc_time - datetime(1970, 1, 1)).total_seconds())
    time_current  = int(datetime.now().timestamp())
    time_remaining = time_notafter - time_current
    if (time_remaining > 300):
        return True
    else:
        return False


def create_metafile(meta_filename, local_filename, size, hash_val, replica_list = []):
    published = str(datetime.now().replace(microsecond=0).isoformat())
    with open(meta_filename, 'w') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write(' <metalink xmlns="urn:ietf:params:xml:ns:metalink">\n')
        f.write("   <published>{}</published>\n".format(published))
        f.write("   <file name=\"{}\">\n".format(local_filename))
        f.write("     <size>{}</size>\n".format(size))
        f.write("     <hash type=\"md5\">{}</hash>\n".format(hash_val))
        for url in replica_list:
            f.write("     <url><![CDATA[{}]]></url>\n".format(url))
        f.write('   </file>\n')
        f.write(' </metalink>\n')
        f.closed


def create_ssl_context():
    global cert, key
    # ssl related options
    if IsValidCert(tokencert):
        cert = tokencert
        key  = tokenkey
    else:
        cert = usercert
        key = userkey

    ctx = ssl.SSLContext()
    verify_mode = ssl.CERT_REQUIRED  # CERT_NONE, CERT_OPTIONAL, CERT_REQUIRED
    ctx.verify_mode = verify_mode
    ctx.check_hostname = False
    ctx.load_verify_locations(capath=capath_default)
    ctx.load_verify_locations(capath=user_globus_dir)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    return ctx


def CreateJsonCommand(command, options=[]):
    cmd_dict = {"command": command, "options": options}
    jcmd = json.dumps(cmd_dict)
    jcmd.encode('ascii', 'ignore')
    return jcmd


def ProcessReceivedMessage(message='', shellcmd = None):
    global json_output, json_meta_output, getToken, currentdir, site, user, ccmd, error, exitcode
    if not message: return
    message.encode('ascii', 'ignore')
    json_dict = json.loads(message)
    currentdir = json_dict["metadata"]["currentdir"]
    user = json_dict["metadata"]["user"]

    error = ''
    if 'error' in json_dict["metadata"]:
        error = json_dict["metadata"]["error"]

    if getToken:  # Processing of token command
        tokencert_content = json_dict['results'][0]["tokencert"]
        if os.path.isfile(tokencert):
            os.chmod(tokencert, 0o700)
        with open(tokencert, "w") as tcert:
            print(f"{tokencert_content}", file=tcert)
            os.chmod(tokencert, 0o400)

        tokenkey_content = json_dict['results'][0]["tokenkey"]
        if os.path.isfile(tokenkey):
            os.chmod(tokenkey, 0o700)
        with open(tokenkey, "w") as tkey:
            print(f"{tokenkey_content}", file=tkey)
            os.chmod(tokenkey, 0o400)
        getToken = bool(False)
        ccmd = ''
        return  # after writing the token files we finished with the message

    if json_output:
        if not json_meta_output:
            if 'metadata' in json_dict: del json_dict['metadata']
        print(json.dumps(json_dict, sort_keys=True, indent=4))
    else:
        websocket_output = '\n'.join(str(item['message']) for item in json_dict['results'])
        if shellcmd:
            # shlex.split(shellcmd)
            # shlex.quote(shellcmd)
            shell_run = subprocess.run(shellcmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, input=websocket_output, encoding='ascii', shell=True, env=os.environ)
            stdout = shell_run.stdout
            if stdout: print(stdout)
            stderr = shell_run.stderr
            if stderr: print(stderr)
        else:
            for item in json_dict['results']:
                print(item['message'])
            if error: print(error)

    # reset the current executed command, the received message was processed
    ccmd = ''


async def ProcessXrootdCp(xrd_copy_command):
    global websocket
    if len(xrd_copy_command) < 2:
        print("at least 2 arguments are needed : src dst")
        print("the command is of the form of (with the strict order of arguments):")
        print("cp args src dst")
        print("where src|dst are local files if prefixed with file:// or grid files otherwise")
        return

    isSrcLocal = bool(False)
    isDstLocal = bool(False)
    isDownload = bool(True)

    # clean up the paths to be used in the xrdcp command
    src = ''
    src_path = ''
    if xrd_copy_command[-2].startswith('file://'):
        isSrcLocal = True
        src = xrd_copy_command[-2].replace("file://", "")
        src = re.sub(r"\/*\.\/+", Path.cwd().as_posix(), src)
        src = re.sub(r"\/*\.\.\/+", Path.cwd().parent.as_posix(), src)
    else:
        src = xrd_copy_command[-2]

    dst = ''
    dst_path = ''
    if xrd_copy_command[-1].startswith('file://'):
        isDstLocal = True
        dst = xrd_copy_command[-1].replace("file://", "")
        dst = re.sub(r"\/*\.\/+", Path.cwd().as_posix() + "/", dst)
        dst = re.sub(r"\/*\.\.\/+", Path.cwd().parent.as_posix() + "/", dst)
    else:
        dst = xrd_copy_command[-1]

    src_path = Path(src)
    dst_path = Path(dst)
    file_name = src_path.name  # no matter the case, the actual proper filename is given by src

    if not (isSrcLocal ^ isDstLocal):
        print("src and dst cannot be both of the same type : one must be local and one grid")
        return

    # process paths for DOWNLOAD
    get_envelope_arg_list = []  # construct command for getting authz envelope
    src_final_path_str = ''
    dst_final_path_str = ''
    if isDstLocal:  # DOWNLOAD FROM GRID
        isDownload = True
        if not dst_path.is_absolute():
            dst_path = Path.cwd()/dst_path
        else:
            dst_path = dst_path
        if dst_path.is_dir(): dst_path = Path.joinpath(dst_path, file_name)
        dst_final_path_str = dst_path.as_posix()

        get_envelope_arg_list.append("read")
        get_envelope_arg_list.append(src_path.as_posix())
    else:  # WRITE TO GRID
        isDownload = False
        if not dst_path.is_absolute():
            dst_path = Path(currentdir)/dst_path
        else:
            dst_path = dst_path
        if dst_path.is_dir(): dst_path = Path.joinpath(dst_path, file_name)

        if not src_path.is_absolute():
            src_path = Path.cwd()/src_path
        else:
            src_path = src_path

        dst_final_path_str = dst_path.as_posix()
        src_final_path_str = src_path.as_posix()

        get_envelope_arg_list.append("write")
        get_envelope_arg_list.append(dst_path.as_posix())

    if XRDDEBUG:
        print(src_path.as_posix())
        print(dst_path.as_posix())
        print(get_envelope_arg_list)
        print("\n")

    access_cmd_json = CreateJsonCommand('access', get_envelope_arg_list)
    await websocket.send(access_cmd_json)
    result = await websocket.recv()
    result.encode('ascii', 'ignore')
    json_dict = json.loads(result)
    #print(json.dumps(json_dict, sort_keys=True, indent=4))

    if not json_dict['results']:
        if json_dict["metadata"]["error"]:
            print("{}".format(json_dict["metadata"]["error"]))
            return

    url_list_src = []
    url_list_dst = []
    if isDownload:
        # multiple replicas are downloaded to a single file
        url_list_4meta = []
        size_4meta = ''
        md5_4meta = ''
        for server in json_dict['results']:
            complete_url = server['url'] + "?" + "authz=" + server['envelope'] + xrdcp_args
            url_list_4meta.append(complete_url)

        url_list_dst.append({"url": dst_final_path_str, "token": ''})  # the local file destination

        size_4meta = json_dict['results'][0]['size']  # size SHOULD be the same for all replicas
        md5_4meta = json_dict['results'][0]['md5']  # the md5 hash SHOULD be the same for all replicas

        meta_fn = src_path.as_posix().replace("/", "_") + ".meta4"
        meta_fn = re.sub("^_", "", meta_fn)
        meta_fn = tmpdir + "/" + meta_fn

        create_metafile(meta_fn, dst_final_path_str, size_4meta, md5_4meta, url_list_4meta)
        del url_list_src[:]
        url_list_src.append({"url": meta_fn, "token": ''})
    else:
        # single file is uploaded to multiple replicas
        for server in json_dict['results']:
            complete_url = server['url'] + "?" + "authz=" + server['envelope'] + xrdcp_args
            url_list_dst.append({"url": complete_url, "token": server['envelope']})
        url_list_src.append({"url": src_final_path_str, "token": ''})

    if XRDDEBUG:
        for url in url_list_src:
            print("src:\n{}".format(url['url']))
            print("src_token:\n{}\n\n".format(url['token']))
        for url in url_list_dst:
            print("dst:\n{}".format(url['url']))
            print("dst_token:\n{}\n\n".format(url['token']))

    token_list_upload_ok = XrdCopy(url_list_src, url_list_dst)  # defer the list of url and files to xrootd processing

    if token_list_upload_ok:  # it was an upload job that had succesfull uploads
        for token in token_list_upload_ok:
            json_commit = CreateJsonCommand('commit', [str(token)])
            await websocket.send(json_commit)
            wb_results = await websocket.recv()
            json_dict = json.loads(wb_results)
            if XRDDEBUG:
                print(json.dumps(json_dict, sort_keys=True, indent=4))


async def JAlienConnect(jsoncmd = ''):
    global websocket, fHostWS, fHostWSUrl, ws_path, currentdir, site, commandlist, getToken, ccmd
    fHostWS = 'wss://' + default_server + ':' + str(fWSPort)
    fHostWSUrl = fHostWS + ws_path
    if str(fHostWSUrl).startswith("wss://"):
        ssl_context = create_ssl_context()
    else:
        ssl_context = None

    if DEBUG: print("Connecting to : ", fHostWSUrl)
    async with websockets.connect(fHostWSUrl, ssl=ssl_context, max_queue = 4, max_size = 16*1024*1024) as websocket:
        # if the certificate used is not the token, then get one
        if cert == usercert:
            getToken = bool(True)
            await websocket.send(CreateJsonCommand('token'))
            result = await websocket.recv()
            ProcessReceivedMessage(result)

        # no matter if command or interactive mode, we need currentdir, user and commandlist
        if not commandlist:
            # get the command list to check validity of commands
            await websocket.send(CreateJsonCommand('commandlist'))
            result = await websocket.recv()
            result = result.lstrip()
            json_dict_list = json.loads("[{}]".format(result.replace('}{', '},{')))
            json_dict = json_dict_list[-1]
            # first executed commands, let's initialize the following (will re-read at each ProcessReceivedMessage)
            commandlist = json_dict["results"][0]["message"]
            currentdir = json_dict["metadata"]["currentdir"]
            user = json_dict["metadata"]["user"]

        if jsoncmd:  # command mode
            ccmd = jsoncmd
            signal.signal(signal.SIGINT, signal_handler)
            json_dict = json.loads(jsoncmd)
            if json_dict["command"].startswith("cp"):  # defer cp processing to ProcessXrootdCp
                await ProcessXrootdCp(json_dict["options"])
            else:
                await websocket.send(jsoncmd)
                result = await websocket.recv()
                ProcessReceivedMessage(result)
        else:        # interactive/shell mode
            while True:
                signal.signal(signal.SIGINT, signal_handler)
                INPUT = ''
                try:
                    INPUT = input(f"jsh: {currentdir} >")
                except EOFError:
                    exit_message()

                if not INPUT: continue
                # if shell command, just run it and return
                if re.match("!", INPUT):
                    sh_cmd = re.sub(r'^!', '', INPUT)
                    #sh_cmd = shlex.quote(sh_cmd)
                    shcmd_out = subprocess.run(sh_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=True, env=os.environ)
                    stdout = shcmd_out.stdout
                    if stdout: print(stdout.decode())
                    stderr = shcmd_out.stderr
                    if stderr: print(stderr.decode())
                    continue

                # process the input and take care of pipe to shell
                input_list = []
                pipe_to_shell_cmd = ''
                if "|" in str(INPUT):  # if we have pipe to shell command
                    input_split_pipe = INPUT.split('|', maxsplit=1)  # split in before pipe (jalien cmd) and after pipe (shell cmd)
                    input_list = input_split_pipe[0].split()  # the list of arguments sent to websocket
                    pipe_to_shell_cmd = input_split_pipe[1]  # the shell command
                    pipe_to_shell_cmd.encode('ascii', 'unicode-escape')
                else:
                    input_list = INPUT.split()

                # process help commands
                cmd = input_list[0]
                input_list.pop(0)  # we have the cmd, so remove from the list

                # defer to cp xrootd function
                if cmd.startswith("cp"):  # defer cp processing to ProcessXrootdCp
                    await ProcessXrootdCp(input_list)
                    continue

                if (cmd == "?") or (cmd == "help"):
                    if len(input_list) > 0:
                        cmdhelp = input_list[0]
                        if cmdhelp in commandlist:
                            input_list.clear()
                            cmd = cmdhelp
                            input_list.append(cmd)
                            input_list.append('-h')
                    else:
                        print(commandlist)
                        continue

                jsoncmd = CreateJsonCommand(cmd, input_list)  # make json with cmd and the list of arguments
                ccmd = jsoncmd  # keep a global copy of the json command that is run
                cmd_hist.append(jsoncmd)
                if DEBUG: print(jsoncmd)

                await websocket.send(jsoncmd)
                result = await websocket.recv()
                result = result.lstrip()
                json_dict_list = json.loads("[{}]".format(result.replace('}{', '},{')))
                result = json.dumps(json_dict_list[-1])
                ProcessReceivedMessage(result, pipe_to_shell_cmd)


if __name__ == '__main__':
    # Let's start the connection
    logger = logging.getLogger('websockets')
    logger.setLevel(logging.ERROR)
    # logger.setLevel(logging.DEBUG)
    logger.addHandler(logging.StreamHandler())

    script_name = sys.argv[0]
    if '_json' in script_name: json_output = bool(True)
    if '_json_all' in script_name: json_meta_output = bool(True)

    cmd = ''
    args = sys.argv

    if len(args) > 1:
        args.pop(0)  # remove script name from arg list
        cmd = args[0]
        args.pop(0)  # ALSO remove command from arg list - remains only command args or empty

    if cmd:
        jsoncmd = CreateJsonCommand(cmd, args)
        if DEBUG: print(jsoncmd)
        asyncio.get_event_loop().run_until_complete(JAlienConnect(jsoncmd))
    else:
        asyncio.get_event_loop().run_until_complete(JAlienConnect())



