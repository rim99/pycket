""" Implementation of linklets

class LinkletInstance(W_Object)
class Linklet(object)

 """

#! /usr/bin/env python
# -*- coding: utf-8 -*-
#

from pycket.expand import readfile_rpython, getkey, mksym
from pycket.interpreter import DefineValues, interpret_one, Context
from pycket.assign_convert import assign_convert
from pycket.values import W_LinkletPrim, W_Procedure, W_Object, W_Bool
from pycket.error import SchemeException
from pycket import pycket_json
from pycket.prims.expose import prim_env, expose, expose_val

@expose("make-instance")
def make_instance(args):#name, data, *vars_vals):
    name = args[0]
    data = args[1]
    vars_vals = args[2:]
    # check if the vars and vals match
    if ((len(vars_vals) % 2) != 0):
        raise SchemeException("Variable names and values do not match : %s" % vars_vals)

    if vars_vals == []:
        vars_vals = {}
    
    return LinkletInstance(name, [], [], vars_vals)

@expose("instance-set-variable-value!")
def instance_set_variable_value(args):
    if len(args) != 4:
        raise SchemeException("Expected 4 arguments, given %s - %s" % (len(args), args))

    instance = args[0]
    name = args[1]
    val = args[2]
    mode = args[3]
    if not isinstance(mode, W_Bool):
        raise NotImplementedError("Handle mode : %s" % mode)

    instance.set_bang_def(name, val)
    

class LinkletInstance(W_Object):
    """
    def export_val(self, id_str):
    def is_defined(self, id_str):
    def is_exported(self, id_str):
    def provide_all_exports_to_prim_env(self):
    def lookup(self, id_str, import_num):
    """

    def __init__(self, name, imported_instances, export_ids, defs):
        self.name = name # for debugging
        self.imported_instances = imported_instances
        # [[...],[...,LinkletInstance,...],...]
        self.export_ids = export_ids # python str's
        self.defs = defs # name-value

    def tostring(self):
        return "Linklet Instance : %s - Importing : %s" % (self.name, self.imported_instances)

    def export_val(self, id_str):
        """ Exports a defined value."""
        return self.is_defined(id_str) and self.defs[id_str]

    def is_defined(self, id_str):
        """ Checks if given id is defined by this instance. """
        if id_str not in self.defs.keys():
            raise SchemeException("%s is not defined in (or through) this instance" % id_str)
        return True

    def is_exported(self, id_str):
        """ Checks if given id is exported by this instance. """
        if id_str not in self.export_ids:
            raise SchemeException("%s is not in the exports of this linklet instance" % id_str)
        return True

    def provide_all_exports_to_prim_env(self):
        """ Puts all exported values to prim_env. """
        for name, value in self.defs.iteritems():
            if name in self.export_ids:
                # W_Closure/W_PromotableClosure
                if isinstance(value, W_Procedure):
                    prim_env[name] = W_LinkletPrim(value)
                else:
                    pass # ??

    def lookup(self, id_str, import_num):
        """ Gets the requested value from the appropriate instance. """
        if import_num < 0:
            return self.export_val(id_str)
        else:
            return self.imported_instances[import_num].lookup(id_str, -1)

    def set_defs(self, defs):
        self.defs = defs

    def set_bang_def(self, name, val):
        self.defs[name] = val
        
    def add_def(self, name, val):
        if name in self.defs:
            raise SchemeException("Duplicate definition : %s" % name)

        self.defs[name] = val
        
    def append_defs(self, new_defs):

        # check if we already have any of the new defs
        for name, val in new_defs.iteritems:
            if name in self.defs:
                raise SchemeException("Duplicate definition : %s" % name)

        self.defs.update(new_defs)

class Linklet(object):
    """
    def instantiate(self, env, imported_instances):

    @staticmethod
    def load_linklet(json_file_name, loader):
    """

    def __init__(self, name, importss, exports, all_forms):
        self.name = name # for debugging
        self.importss = importss # list of list of python str
        self.exports = exports # list of python str
        self.forms = all_forms # list of pycket asts

    def instantiate(self, env, imported_instances):
        """ Instantiates the linklet:
        --- takes the imported linklet instances
        --- extracts the specified set of variables
        --- returns a LinkletInstance
        """
        l_importss = len(self.importss)
        l_given_instances = len(imported_instances)
        if l_importss != l_given_instances:
            raise SchemeException("Required %s instances but given %s" % (l_importss, l_given_instances))
        # Check if the imports are really exported by the given instances
        for index in range(l_importss):
            imported_ids = self.importss[index]
            for id_ in imported_ids:
                inst = imported_instances[index]
                assert inst.is_defined(id_) and inst.is_exported(id_)

        inst = LinkletInstance(self.name, imported_instances, self.exports, {})
        env.current_linklet_instance = inst

        for form in self.forms:
            if isinstance(form, DefineValues):
                expression = form.rhs
                values = interpret_one(expression, env).get_all_values()
                len_values = len(values)
                if len(form.names) == len_values:
                    for index in range(len_values):
                        name = form.names[index]
                        value = values[index]

                        inst.add_def(name, value)
                else:
                    raise SchemeException("wrong number of values for define-values")

            else: # any expression
                values = interpret_one(form, env)
                continue

        return inst

    @staticmethod # json_file_name -> Linklet
    def load_linklet(json_file_name, loader):
        """ Expands and loads a linklet from a JSON file"""
        data = readfile_rpython(json_file_name+".linklet")
        json = pycket_json.loads(data)
        assert json.is_object
        json_python_dict = json.value_object()
        assert "linklet" in json_python_dict
        linklet_dict = getkey(json_python_dict, "linklet", type='o')
        assert "exports" in linklet_dict and "body" in linklet_dict # and "importss" in linklet_dict

        # list of JsonObject
        exports_list = getkey(linklet_dict, "exports", type='a')
        # list of python string
        exports = [mksym(sym.value_object()['quote']) for sym in exports_list]

        imports_list = getkey(linklet_dict, "importss", type='a')

        importss = []
        if "importss" in linklet_dict:
            for imports in imports_list:
                arr = imports.value_array()
                importss.append([mksym(sym.value_object()['quote']) for sym in arr])

        all_forms = []
        for body_form in getkey(linklet_dict, "body", type='a'):
            form = loader.to_ast(body_form)
            form = Context.normalize_term(form)
            form = assign_convert(form)
            form.clean_caches()
            all_forms.append(form)

        return Linklet(json_file_name, importss, exports, all_forms)
