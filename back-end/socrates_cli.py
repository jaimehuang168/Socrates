#!/usr/bin/python
'''
Example run:
python socrates_cli.py --param param.json
'''
import traceback
from datetime import datetime
import sys
import os
import argparse
import modules
from pprint import pprint
import json
from translation import *
from pymongo import MongoClient
from bson import objectid
from bson.objectid import ObjectId
import user

origStdout = os.dup(1)
origStderr = os.dup(2)

'''
This output redirection was guess and check
reference: http://stackoverflow.com/questions/4675728/redirect-stdout-to-a-file-in-python/22434262#22434262
I'd like for this to be cleaner
'''

def _redirectOutput():
    try:
        #why? - to check if we have write permissions 
        f = open("logs/python.out.log", "a")
        f.close()
        f = open("logs/python.err.log", "a")
        f.close()

        os.close(1)
        os.open("logs/python.out.log", os.O_WRONLY|os.O_APPEND) #goes for lowest available (hence fd 1)
        os.close(2)
        os.open("logs/python.err.log", os.O_WRONLY|os.O_APPEND)
    except IOError as e:
        print "I/O error on log redirection: %s" % (e.strerror)
        sys.exit(1) #without error logging, all hope is lost

def _restoreOutput():
    sys.stdout.flush()
    sys.stderr.flush()
    os.close(1)
    os.dup(origStdout) #opens on fd 1 ?
    os.close(origStdout)
    os.close(2)
    os.dup(origStderr)
    os.close(origStderr)

def _err(msg):
    return json.dumps({
    'error' : 'true',
    'message': msg
    })


def _parseCSV(csv_str):
    rows = csv_str.split('\n')
    keys = rows[0].split(",")
    working_set = {
        "meta" : {},
        "data" : []
    }
    for k in keys:
        working_set["meta"][k] = "text" #for now, assume text field
    for r in rows[1:]:
        row_obj = {}
        row_parts = r.split(",")
        for i in range(len(row_parts)):
            row_obj[keys[i]] = row_parts[i]
        working_set["data"].append(row_obj)
    return working_set

'''
The run method ties together the running of operators.
It will run the operator, store results, and return appropriate data to be sent back to user.
It will store/retrieve stored collection data
'''
def run(typ, mod, fn, param, working_set=None):
    is_new = False
    if typ in MODULE_LIST and mod in MODULE_LIST[typ]:
        #get module/function references
        callingTyp = getattr(modules, typ)
        callingMod = getattr(callingTyp, mod)
        callingFn = getattr(callingMod, fn)
        fn_specs = callingMod.SPECS['functions']
        #validate parameters from constraints
        if enforceAndConvert(param, fn_specs[fn]['param'], working_set) is False:
            return _err("Parameters are not valid") #get better error from constraint function

        applyDefaults(param, fn_specs[fn]['param'])

        #call and augment with meta information
        if typ == 'analysis':
            if working_set is None:
                return _err("Data not provided")
            is_new = False
            results = callingFn(working_set, param)
            if 'aggregate_result' in fn_specs[fn]:
                results['aggregate_meta'] = fn_specs[fn]['aggregate_result']
            if 'entry_result' in fn_specs[fn]:
                results['entry_meta'] = fn_specs[fn]['entry_result']
            if 'analysis' in working_set:
                working_set['analysis'].append(results)
            else:
                working_set['analysis'] = [results]
        elif typ == 'collection':
            #check for long term
            if 'campaign' in fn_specs[fn]:
                campaign_meta = None
                if working_set is not None:
                    campaign_meta = working_set["meta"]["campaign"]
                    is_new = False
                else:
                    is_new = True

                data = callingFn(param, campaign_meta) #campaign_meta may be none if first trial
                if is_new:
                    working_set = {
                        'data' : data, #only if specified
                        'meta' : fn_specs[fn]['returns'],
                        'input' : param
                    }
                else:
                    working_set.data.extend(data)
            else:
                is_new = True
                data = callingFn(param)
                working_set = {
                    'data' : data, #only if specified
                    'meta' : fn_specs[fn]['returns'],
                    'input' : param
                    }
        return (working_set, is_new)
    return (None, False)

def parse_params(parameters):
    try:
        client = MongoClient()
        mongodb = client.socrates

        result = "{}" #string result from each run-type to print at the end
        working_set = None
        working_set_id = -1
        working_set_name = "Untitled"


        if 'username' in parameters and 'password' in parameters:
            if 'register' in parameters:
                #register new user
                taken = not user.register(parameters['username'], parameters['password'])
                result = json.dumps({"attempted": True, "taken" : taken})
            else:
                #authenticate
                if not user.authenticate(parameters['username'], parameters['password']):
                    return _err("Invalid username and password")
        else:
            #use default user
            user.setDefault()

        if 'working_set_id' in parameters:
            working_set_id = parameters['working_set_id']
            working_set = user.getWorkingSet(working_set_id)
            working_set_name = working_set["working_set_name"]

        if 'working_set_name' in parameters:
            working_set_name = parameters['working_set_name']

        #mutually exclusive if-elif
        if 'module' in parameters:
            typ = parameters['type']
            mod = parameters['module']
            fn = parameters['function']

            print "Running %s, %s, %s\n" % (typ, mod, fn)
            param = {}
            if "input" in parameters:
                param = parameters["input"]
            return_all_data = False
            if "return_all_data" in parameters:
                return_all_data = parameters["return_all_data"]

            if typ == "analysis" and working_set is None:
                return _err("Working set id not included")

            (working_set, is_new) = run(typ, mod, fn, param, working_set)

            user.log_run(typ, mod, fn)

            if working_set is None:
                return _err("Internal operation error")
            if 'error' in working_set and working_set['error']:
                return _err("error: " + working_set['message'])

            #store new/modified working set
            working_set["working_set_name"] = working_set_name
            if is_new:
                insert_id = user.addWorkingSet(working_set)
                working_set['working_set_id'] = str(insert_id)
            else:
                user.updateWorkingSet(working_set_id, working_set) #overwrite in database
                working_set['working_set_id'] = str(working_set_id)

            if not return_all_data:
                #remove all data except first entry
                working_set["data"] = working_set["data"][0:1]
                if "analysis" in working_set:
                    for i in range(len(working_set["analysis"])):
                        a = working_set["analysis"][i]
                        if "entry_analysis" in a:
                            for p in a['entry_analysis']:
                                a['entry_analysis'][p] = a['entry_analysis'][p][0:1]

            return json.dumps(working_set)

        elif 'specs' in parameters:
            print "Fetching specs\n"
            return json.dumps(getAllSpecs())

        elif 'fetch' in parameters:
            working_set = user.getWorkingSet(working_set_id)
            working_set['working_set_id'] = str(working_set_id)
            return json.dumps(working_set)

        elif 'remove' in parameters:
            user.removeWorkingSet(working_set_id)
            return json.dumps({"status" : "success"})

        elif 'rename' in parameters:
            user.renameWorkingSet(working_set_id, parameters['new_name'])
            return json.dumps({"status" : "success"})

        elif 'fetch_all_ids' in parameters:
            working_sets = user.getWorkingSets()
            working_set_identifiers = []
            for w in working_sets:
                wid = {
                    "id" : w["working_set_id"],
                    "name" : "Untitled"
                }
                if "working_set_name" in w:
                    wid["name"] = w["working_set_name"]

                working_set_identifiers.append(wid)

            return json.dumps({
                "ids" : working_set_identifiers
                })
            
        elif 'upload' in parameters:
            working_set = None
            default_working_set_name = "Imported Dataset"
            data = parameters['working_set_data']
            format = parameters['format']
            if format not in ["csv", "json"]:
                return _err("Invalid upload format selected")
            if format == "json":
                try:
                    working_set = json.loads(data) #no additional parsing necessary
                except ValueError as ve:
                    return _err("Working set JSON could not be parsed")
            elif format == "csv":
                #parse string as csv
                working_set = _parseCSV(data)

            if working_set is not None:
                if "working_set_name" not in working_set:
                    working_set["working_set_name"] = default_working_set_name
                working_set_id = user.addWorkingSet(working_set)
                return json.dumps({
                    "id" : str(working_set_id),
                    "name" : working_set["working_set_name"]
                    })

            else:
                return _err("Could not upload")
        return result

    except Exception as e:
        sys.stderr.write("Exception caught: %s\n" % e)
        sys.stderr.write("Stack trace:\n%s\n" % traceback.format_exc())

def init():
    parser = argparse.ArgumentParser(description="SOCRATES Social media data collection, analysis, and exploration")
    parser.add_argument("--param", help="Reference to parameter file", default=False)
    parser.add_argument("--log", help="Redirects all stderr and stdout to logs, only prints working_set", action="store_true")
    args = parser.parse_args()

    parameters = None
    if args.param:
        param_file = open(args.param, "r")
        parameters = json.load(param_file)
        param_file.close()
    if not parameters:
        return _err("No parameter file passed")

    if args.log:
        _redirectOutput() #if any stdout/stderr from modules occurs, log it (used for API logging)
        dateStr = "--start-%s--\n" % datetime.now()
        sys.stderr.write(dateStr)
        sys.stdout.write(dateStr)
    
    result = parse_params(parameters)

    if args.log:
        sys.stdout.write("--end--\n")
        sys.stderr.write("--end--\n")
        _restoreOutput()
    return result

    
str_result = init()
print str_result