from pycket import values
from pycket import vector as values_vector
from pycket.cont import continuation, label
from pycket.error import SchemeException
from pycket.prims.expose import make_call_method
from pycket.small_list import inline_small_list
from rpython.rlib import jit

PREFAB = values.W_Symbol.make("prefab")

# TODO: inspector currently does nothing
class W_StructInspector(values.W_Object):
    errorname = "struct-inspector"
    _immutable_fields_ = ["super"]

    @staticmethod
    def make(inspector, issibling = False):
        super = inspector
        if issibling:
            super = inspector.super if inspector is not None else None
        return W_StructInspector(super)

    def __init__(self, super):
        self.super = super

current_inspector = W_StructInspector(None)

class W_StructType(values.W_Object):
    errorname = "struct-type-descriptor"
    _immutable_fields_ = ["name", "super", "init_field_cnt", \
        "auto_field_cnt", "total_field_cnt", "auto_v", "props", "inspector", \
        "immutables", "guard", "constr_name", "auto_values[*]", "offsets[*]",
        "constr", "pred", "acc", "mut"]
    unbound_prefab_types = {}

    @staticmethod
    def make(name, super_type, init_field_cnt, auto_field_cnt,
             auto_v=values.w_false, props=values.w_null,
             inspector=values.w_false, proc_spec=values.w_false,
             immutables=values.w_null, guard=values.w_false,
             constr_name=values.w_false, env=None, cont=None):
        """
        This method returns five instances:
            W_StructType
            W_StructConstructor
            W_StructPredicate
            W_StructAccessor
            W_StructMutator
        """
        w_struct_type = W_StructType.make_simple(name, super_type,
            init_field_cnt, auto_field_cnt, auto_v, props, inspector,
            proc_spec, immutables, guard, constr_name)
        return w_struct_type.initialize_props(props, proc_spec, env, cont)

    @staticmethod
    def make_simple(name, super_type, init_field_cnt, auto_field_cnt,
            auto_v=values.w_false, props=values.w_null,
            inspector=values.w_false, proc_spec=values.w_false,
            immutables=values.w_null, guard=values.w_false,
            constr_name=values.w_false):
        """
        This method returns an instance of W_StructType only.
        It does not support properties.
        """
        if inspector is PREFAB:
            prefab_key = W_PrefabKey.from_raw_params(name, init_field_cnt,\
                auto_field_cnt, auto_v, immutables, super_type)
            if prefab_key in W_StructType.unbound_prefab_types:
                return W_StructType.unbound_prefab_types.pop(prefab_key)
        return W_StructType(name, super_type, init_field_cnt, auto_field_cnt,
            auto_v, inspector, proc_spec, immutables, guard, constr_name)

    @staticmethod
    def make_prefab(prefab_key):
        if prefab_key in W_StructType.unbound_prefab_types:
            w_struct_type = W_StructType.unbound_prefab_types[prefab_key]
        else:
            name, init_field_cnt, auto_field_cnt, auto_v, mutables, super_key =\
                prefab_key.make_key_tuple()
            super_type = W_StructType.make_prefab(super_key) if super_key else\
                values.w_false
            immutables = []
            for i in range(init_field_cnt):
                if i not in mutables:
                    immutables.append(values.W_Fixnum(i))
            w_struct_type = W_StructType.make_simple(values.W_Symbol.make(name),
                super_type, values.W_Fixnum(init_field_cnt),
                values.W_Fixnum(auto_field_cnt), auto_v, values.w_null,
                PREFAB, values.w_false, values.to_list(immutables))
            W_StructType.unbound_prefab_types[prefab_key] = w_struct_type
        return w_struct_type

    @continuation
    def save_prop_value(self, props, idx, is_checked, env, cont, _vals):
        from pycket.interpreter import check_one_val
        prop = props[idx][0]
        prop_val = check_one_val(_vals)
        props[idx] = (prop, prop_val, None)
        return self.attach_prop(props, idx, is_checked, env, cont)

    @label
    def attach_prop(self, props, idx, is_checked, env, cont):
        from pycket.interpreter import return_multi_vals
        if idx < len(props):
            (prop, prop_val, sub_prop) = props[idx]
            if sub_prop is not None:
                for p in props:
                    if p[0] is sub_prop:
                        return prop_val.call([p[1]], env,
                            self.save_prop_value(props, idx, False, env, cont))
            assert isinstance(prop, W_StructProperty)
            if not is_checked and prop.guard.iscallable():
                return prop.guard.call([prop_val, values.to_list(self.struct_type_info())],
                    env, self.save_prop_value(props, idx, True, env, cont))
            self.props.append((prop, prop_val))
            return self.attach_prop(props, idx + 1, False, env, cont)
        # at this point all properties are saved, next step is to copy
        # propertyes from super types
        struct_type = self.super
        while isinstance(struct_type, W_StructType):
            self.props = self.props + struct_type.props
            if self.prop_procedure is None:
                self.prop_procedure = struct_type.prop_procedure
                self.procedure_source = struct_type
            struct_type = struct_type.super
        struct_tuple = self.make_struct_tuple()
        return return_multi_vals(values.Values.make(struct_tuple), env, cont)

    @jit.unroll_safe
    def initialize_prop(self, props, p, sub_prop=None):
        prop = p.car()
        prop_val = p.cdr()
        if sub_prop is None:
            if prop.isinstance(w_prop_procedure):
                if self.prop_procedure is not None:
                    raise SchemeException("duplicate property binding")
                self.prop_procedure = prop_val
                self.procedure_source = self
            elif prop.isinstance(w_prop_checked_procedure):
                if self.total_field_cnt < 2:
                    raise SchemeException("need at least two fields in the structure type")
        props.append((prop, prop_val, sub_prop))
        assert isinstance(prop, W_StructProperty)
        for super_p in prop.supers:
            self.initialize_prop(props, super_p, prop)

    @jit.unroll_safe
    def initialize_props(self, props, proc_spec, env, cont):
        """
        Properties initialisation contains few steps:
            1. call initialize_prop for each property from the input list,
               it extracts all super values and stores them into props array
               with a flat structure
            2. recursively call attach_prop for each property from props and
               prepare the value:
               * if the current property has a subproperty, the value is the result
                 of calling value procedure with a sub value as an argument
               * if the current property has a guard, the value is the result of
                 calling guard with a value and struct type info as arguments
               * in other case just keep the current value
        """
        proplist = values.from_list(props)
        props = []
        for p in proplist:
            self.initialize_prop(props, p)
        if proc_spec is not values.w_false:
            self.initialize_prop(props, values.W_Cons.make(w_prop_procedure, proc_spec))
        return self.attach_prop(props, 0, False, env, cont)

    def __init__(self, name, super_type, init_field_cnt, auto_field_cnt,
            auto_v, inspector, proc_spec, immutables, guard, constr_name):
        self.name = name.value
        self.super = super_type
        self.init_field_cnt = init_field_cnt.value
        self.auto_field_cnt = auto_field_cnt.value
        self.total_field_cnt = self.init_field_cnt + self.auto_field_cnt + \
            (super_type.total_field_cnt if isinstance(super_type, W_StructType) else 0)
        self.auto_v = auto_v
        self.props = []
        self.prop_procedure = None
        self.procedure_source = None
        self.inspector = inspector
        self.immutables = []
        if isinstance(proc_spec, values.W_Fixnum):
            self.immutables.append(proc_spec.value)
        for i in values.from_list(immutables):
            assert isinstance(i, values.W_Fixnum)
            self.immutables.append(i.value)
        self.guard = guard
        if isinstance(constr_name, values.W_Symbol):
            self.constr_name = constr_name.value
        else:
            self.constr_name = "make-" + self.name

        self.auto_values = [self.auto_v] * self.auto_field_cnt
        self.offsets = self.calculate_offsets()
        self.isprefab = self.inspector is PREFAB
        if self.isprefab:
            self.isopaque = False
        else:
            self.isopaque = self.inspector is not values.w_false

        self.constr = W_StructConstructor(self)
        self.pred = W_StructPredicate(self)
        self.acc = W_StructAccessor(self)
        self.mut = W_StructMutator(self)

    @jit.unroll_safe
    def calculate_offsets(self):
        result = {}
        struct_type = self
        while isinstance(struct_type, W_StructType):
            result[struct_type] = struct_type.total_field_cnt - \
            struct_type.init_field_cnt - struct_type.auto_field_cnt
            struct_type = struct_type.super
        return result

    @jit.elidable
    def get_offset(self, type):
        return self.offsets.get(type, -1)

    def struct_type_info(self):
        name = values.W_Symbol.make(self.name)
        init_field_cnt = values.W_Fixnum(self.init_field_cnt)
        auto_field_cnt = values.W_Fixnum(self.auto_field_cnt)
        immutable_k_list = values.to_list([values.W_Fixnum(i) for i in self.immutables])
        # TODO: value of the super variable should be a structure type descriptor
        # for the most specific ancestor of the type that is controlled by the current inspector,
        # or #f if no ancestor is controlled by the current inspector
        super = self.super
        # TODO: #f if the seventh result is the most specific ancestor type or
        # if the type has no supertype, #t otherwise
        skipped = values.w_false
        return [name, init_field_cnt, auto_field_cnt, self.acc, self.mut,
                immutable_k_list, super, skipped]

    def make_struct_tuple(self):
        return [self, self.constr, self.pred, self.acc, self.mut]

    def tostring(self):
        return "#<struct-type:%s>" % self.name

class W_PrefabKey(values.W_Object):
    _immutable_fields_ = ["name", "init_field_cnt", "auto_field_cnt",\
        "auto_v", "mutables", "super_key"]
    all_keys = []

    @staticmethod
    def make(name, init_field_cnt, auto_field_cnt, auto_v, mutables, super_key):
        for key in W_PrefabKey.all_keys:
            if key.equal((name, init_field_cnt, auto_field_cnt, auto_v,\
                mutables, super_key)):
                return key
        key = W_PrefabKey(name, init_field_cnt, auto_field_cnt, auto_v,\
            mutables, super_key)
        W_PrefabKey.all_keys.append(key)
        return key

    @staticmethod
    def from_struct_type(struct_type):
        assert isinstance(struct_type, W_StructType)
        name = struct_type.name
        init_field_cnt = struct_type.init_field_cnt
        auto_field_cnt = struct_type.auto_field_cnt
        auto_v = struct_type.auto_v
        mutables = []
        prev_idx = 1
        for i in struct_type.immutables:
            for j in range(prev_idx, i):
                mutables.append(j)
            prev_idx = i + 1
        super_key = W_PrefabKey.from_struct_type(struct_type.super) if\
            struct_type.super is not values.w_false else None
        return W_PrefabKey.make(name, init_field_cnt, auto_field_cnt, auto_v,\
            mutables, super_key)

    @staticmethod
    def from_raw_params(w_name, w_init_field_cnt, w_auto_field_cnt, auto_v,\
        w_immutables, super_type):
        assert isinstance(w_name, values.W_Symbol)
        name = w_name.value
        assert isinstance(w_init_field_cnt, values.W_Fixnum)
        init_field_cnt = w_init_field_cnt.value
        assert isinstance(w_auto_field_cnt, values.W_Fixnum)
        auto_field_cnt = w_auto_field_cnt.value
        mutables = []
        prev_idx = 1
        for i in values.from_list(w_immutables):
            assert isinstance(i, values.W_Fixnum)
            for j in range(prev_idx, i.value):
                mutables.append(j)
            prev_idx = i.value + 1
        super_key = W_PrefabKey.from_struct_type(super_type) if\
            super_type is not values.w_false else None
        return W_PrefabKey.make(name, init_field_cnt, auto_field_cnt, auto_v,\
            mutables, super_key)

    @staticmethod
    def from_raw_key(w_key, total_field_cnt=0):
        init_field_cnt = -1
        auto_field_cnt = 0
        auto_v = values.w_false
        super_key = None
        mutables = []
        if isinstance(w_key, values.W_Symbol):
            name = w_key.value
            init_field_cnt = total_field_cnt
        else:
            key = values.from_list(w_key)
            w_name = key[0]
            assert isinstance(w_name, values.W_Symbol)
            name = w_name.value
            idx = 1
            w_init_field_cnt = key[idx]
            if isinstance(w_init_field_cnt, values.W_Fixnum):
                init_field_cnt = w_init_field_cnt.value
                idx += 1
            if len(key) > idx:
                w_auto = key[idx]
                if isinstance(w_auto, values.W_Cons):
                    auto = values.from_list(w_auto)
                    w_auto_field_cnt = auto[0]
                    assert isinstance(w_auto_field_cnt, values.W_Fixnum)
                    auto_field_cnt = w_auto_field_cnt.value
                    auto_v = auto[1]
                    idx += 1
            if len(key) > idx:
                v = key[idx]
                if isinstance(v, values_vector.W_Vector):
                    for i in range(v.len):
                        mutable = v.ref(i)
                        assert isinstance(mutable, values.W_Fixnum)
                        mutables.append(mutable.value)
                    idx += 1
            if len(key) > idx:
                w_super_key = values.to_list(key[idx:])
                super_key = W_PrefabKey.from_raw_key(w_super_key)
        if init_field_cnt == -1:
            init_field_cnt = total_field_cnt
            s_key = super_key
            while s_key:
                super_name, super_init_field_cnt, super_auto_field_cnt,\
                    super_auto_v, super_mutables, s_key = s_key.make_key_tuple()
                init_field_cnt -= super_init_field_cnt
        return W_PrefabKey.make(name, init_field_cnt, auto_field_cnt, auto_v,\
            mutables, super_key)

    @staticmethod
    def is_prefab_key(v):
        if isinstance(v, values.W_Symbol):
            return values.w_true
        elif isinstance(v, values.W_Cons):
            key = values.from_list(v)
            if not isinstance(key[0], values.W_Symbol):
                return values.w_false
            idx = 1
            if isinstance(key[idx], values.W_Fixnum):
                idx += 1
            else:
                if not isinstance(key[idx], values.W_Cons):
                    return values.w_false
                idx += 1
            if len(key) > idx:
                if not isinstance(key[idx], values.W_Cons):
                    return values.w_false
                idx += 1
            if len(key) > idx:
                if not isinstance(key[idx], values_vector.W_Vector):
                    return values.w_false
                idx += 1
            if len(key) > idx:
                return W_PrefabKey.is_prefab_key(key[idx])
            return values.w_true
        else:
            return values.w_false

    def __init__(self, name, init_field_cnt, auto_field_cnt, auto_v,\
        mutables, super_key):
        self.name = name
        self.init_field_cnt = init_field_cnt
        self.auto_field_cnt = auto_field_cnt
        self.auto_v = auto_v
        self.mutables = mutables
        self.super_key = super_key

    def equal(self, other):
        if isinstance(other, W_PrefabKey):
            return self.make_key_tuple() == other.make_key_tuple()
        elif isinstance(other, tuple):
            return self.make_key_tuple() == other
        return False

    def key(self):
        key = []
        key.append(values.W_Symbol.make(self.name))
        key.append(values.W_Fixnum(self.init_field_cnt))
        if self.auto_field_cnt > 0:
            key.append(values.to_list(\
                [values.W_Fixnum(self.auto_field_cnt), self.auto_v]))
        mutables = []
        for i in self.mutables:
            mutables.append(values.W_Fixnum(i))
        if mutables:
            key.append(values_vector.W_Vector.fromelements(mutables))
        if self.super_key:
            key.extend(self.super_key.key())
        return key

    def short_key(self):
        key = self.key()
        short_key = key[:1] + key[2:]
        return values.to_list(short_key) if len(short_key) > 1 else key[0]

    def make_key_tuple(self):
        return self.name, self.init_field_cnt, self.auto_field_cnt,\
            self.auto_v, self.mutables, self.super_key

class W_RootStruct(values.W_Object):
    errorname = "root-struct"

    def __init__(self):
        raise NotImplementedError("abstract base class")

    def iscallable(self):
        return self.struct_type().prop_procedure is not None

    @continuation
    def arity_error_cont(self, env, cont, _vals):
        from pycket.interpreter import check_one_val
        msg = check_one_val(_vals)
        raise SchemeException("expected: " + msg.tostring())

    @continuation
    def receive_proc_cont(self, args, env, cont, _vals):
        from pycket.interpreter import check_one_val
        proc = check_one_val(_vals)
        return self.checked_call(proc, args, env, cont)

    def checked_call(self, proc, args, env, cont):
        args_len = len(args)
        (arities, rest) = proc.get_arity()
        if args_len < rest and args_len not in arities:
            for w_prop, w_prop_val in self.struct_type().props:
                if w_prop.isinstance(w_prop_arity_string):
                    return w_prop_val.call([self], env, self.arity_error_cont(env, cont))
        return proc.call(args, env, cont)

    # For all subclasses, it should be sufficient to implement ref, set, and
    # struct_type for call and iscallable to work properly.
    @label
    @make_call_method(simple=False)
    def call(self, args, env, cont):
        typ = self.struct_type()
        proc = typ.prop_procedure
        if isinstance(proc, values.W_Fixnum):
            return self.ref(typ.procedure_source, proc.value, env,
                    self.receive_proc_cont(args, env, cont))
        args = [self] + args
        return self.checked_call(proc, args, env, cont)

    def struct_type(self):
        raise NotImplementedError("abstract base class")

    @label
    def ref(self, struct_type, field, env, cont):
        raise NotImplementedError("abstract base class")

    @label
    def set(self, struct_type, field, val, env, cont):
        raise NotImplementedError("abstract base class")

    @label
    def get_prop(self, property, env, cont):
        raise NotImplementedError("abstract base class")

    def vals(self):
        raise NotImplementedError("abstract base class")

@inline_small_list(immutable=False, attrname="values")
class W_Struct(W_RootStruct):
    errorname = "struct"
    _immutable_fields_ = ["_type", "values"]

    def __init__(self, type):
        self._type = type

    @staticmethod
    def make_prefab(w_key, w_values):
        w_struct_type = W_StructType.make_prefab(W_PrefabKey.from_raw_key(w_key, len(w_values)))
        return W_Struct.make(w_values, w_struct_type)

    def vals(self):
        return self._get_full_list()

    def struct_type(self):
        return self._type

    # Rather than reference functions, we store the continuations. This is
    # necessarray to get constant stack usage without adding extra preamble
    # continuations.
    @label
    def ref(self, type, field, env, cont):
        from pycket.interpreter import return_value
        offset = jit.promote(self._type).get_offset(type)
        if offset == -1:
            raise SchemeException("cannot reference an identifier before its definition")
        value = self._get_list(field + offset)
        if isinstance(value, values.W_Cell):
            value = value.get_val()
        return return_value(value, env, cont)

    @label
    def set(self, type, field, val, env, cont):
        from pycket.interpreter import return_value
        offset = jit.promote(self._type).get_offset(type)
        if offset == -1:
            raise SchemeException("cannot reference an identifier before its definition")
        value = self._get_list(field + offset)
        if isinstance(value, values.W_Cell):
            value.set_val(val)
        else:
            self._set_list(field + offset, values.W_Cell(val))
        return return_value(values.w_void, env, cont)

    # unsafe versions
    def _ref(self, k):
        value = self._get_list(k)
        if isinstance(value, values.W_Cell):
            return value.get_val()
        return value
    def _set(self, k, val):
        value = self._get_list(k)
        if isinstance(value, values.W_Cell):
            value.set_val(val)
        else:
            self._set_list(k, values.W_Cell(val))

    # We provide a method to get properties from a struct rather than a struct_type,
    # since impersonators can override struct properties.
    @label
    def get_prop(self, property, env, cont):
        from pycket.interpreter import return_value
        for p, val in self._type.props:
            if p is property:
                return return_value(val, env, cont)
        raise SchemeException("%s-accessor: expected %s? but got %s" %
            (property.name, property.name, self.tostring()))

    # TODO: currently unused
    def tostring_proc(self, env, cont):
        for w_prop, w_val in self._type.props:
            if w_prop.isinstance(w_prop_custom_write):
                assert isinstance(w_val, values_vector.W_Vector)
                w_write_proc = w_val.ref(0)
                port = values.W_StringOutputPort()
                # TODO: #t for write mode, #f for display mode,
                # or 0 or 1 indicating the current quoting depth for print mode
                mode = values.w_false
                return w_write_proc.call([self, port, mode], env, cont)
        return self.tostring()

    def tostring(self):
        if self._type.isopaque:
            result =  "#<%s>" % self._type.name
        else:
            if self._type.isprefab:
                result = "#s(%s %s)" %\
                    (W_PrefabKey.from_struct_type(self._type).short_key()\
                        .tostring(), ' '.join([val.tostring() for val in self.vals()]))
            else:
                result = "(%s %s)" % (self._type.name,\
                    ' '.join([val.tostring() for val in self.vals()]))
        return result

class W_StructConstructor(values.W_Procedure):
    _immutable_fields_ = ["type"]
    def __init__(self, type):
        self.type = type

    def make_struct(self, field_values):
        raise NotImplementedError("abstract base class")

    @continuation
    def constr_proc_cont(self, field_values, env, cont, _vals):
        from pycket.interpreter import return_value
        checked_values = _vals._get_full_list()
        if checked_values:
            field_values = checked_values + field_values[len(checked_values):]
        if len(self.type.auto_values) > 0:
            field_values = field_values + self.type.auto_values
        result = W_Struct.make(field_values, self.type)
        return return_value(result, env, cont)

    @jit.unroll_safe
    def _splice(self, array, array_len, index, insertion, insertion_len):
        new_len = array_len + insertion_len
        assert new_len >= 0
        new_array = [None] * new_len
        for pre_index in range(index):
            new_array[pre_index] = array[pre_index]
        for insert_index in range(insertion_len):
            new_array[index + insert_index] = insertion[insert_index]
        for post_index in range(index, array_len):
            new_array[post_index + insertion_len] = array[post_index]
        return new_array

    @continuation
    def constr_proc_wrapper_cont(self, field_values, issuper, env, cont, _vals):
        from pycket.interpreter import return_multi_vals, jump
        checked_values = _vals._get_full_list()
        if checked_values:
            field_values = checked_values
        super_type = jit.promote(self.type.super)
        if isinstance(super_type, W_StructType):
            split_position = len(field_values) - self.type.init_field_cnt
            super_auto = super_type.constr.type.auto_values
            assert split_position >= 0
            field_values = self._splice(field_values, len(field_values),\
                split_position, super_auto, len(super_auto))
            return super_type.constr.code(field_values[:split_position], True,
                env, self.constr_proc_cont(field_values, env, cont))
        else:
            if issuper:
                return return_multi_vals(values.Values.make(field_values), env, cont)
            else:
                return jump(env, self.constr_proc_cont(field_values, env, cont))

    def code(self, field_values, issuper, env, cont):
        from pycket.interpreter import jump
        if self.type.guard is values.w_false:
            return jump(env, self.constr_proc_wrapper_cont(field_values, issuper, env, cont))
        else:
            guard_args = field_values + [values.W_Symbol.make(self.type.name)]
            jit.promote(self)
            return self.type.guard.call(guard_args, env,
                self.constr_proc_wrapper_cont(field_values, issuper, env, cont))

    @label
    @make_call_method(simple=False)
    def call(self, args, env, cont):
        return self.code(args, False, env, cont)

    def tostring(self):
        return "#<procedure:%s>" % self.type.name

class W_StructPredicate(values.W_Procedure):
    errorname = "struct-predicate"
    _immutable_fields_ = ["type"]
    def __init__ (self, type):
        self.type = type

    @make_call_method([values.W_Object])
    def call(self, struct):
        if isinstance(struct, W_RootStruct):
            struct_type = struct.struct_type()
            while isinstance(struct_type, W_StructType):
                if struct_type is self.type:
                    return values.w_true
                struct_type = struct_type.super
        return values.w_false

    def tostring(self):
        return "#<procedure:%s?>" % self.type.name

class W_StructFieldAccessor(values.W_Procedure):
    errorname = "struct-field-accessor"
    _immutable_fields_ = ["accessor", "field"]
    def __init__ (self, accessor, field, field_name):
        assert isinstance(accessor, W_StructAccessor)
        self.accessor = accessor
        self.field = field
        self.field_name = field_name

    @label
    @make_call_method([W_RootStruct], simple=False)
    def call(self, struct, env, cont):
        return self.accessor.access(struct, self.field, env, cont)

    def tostring(self):
        return "#<procedure:%s-%s>" % (self.accessor.type.name, self.field_name)

class W_StructAccessor(values.W_Procedure):
    errorname = "struct-accessor"
    _immutable_fields_ = ["type"]
    def __init__ (self, type):
        self.type = type

    def access(self, struct, field, env, cont):
        return struct.ref(self.type, field.value, env, cont)

    call = label(make_call_method([W_RootStruct, values.W_Fixnum], simple=False)(access))

    def tostring(self):
        return "#<procedure:%s-ref>" % self.type.name

class W_StructFieldMutator(values.W_Procedure):
    errorname = "struct-field-mutator"
    _immutable_fields_ = ["mutator", "field"]
    def __init__ (self, mutator, field, field_name):
        assert isinstance(mutator, W_StructMutator)
        self.mutator = mutator
        self.field = field
        self.field_name = field_name

    @label
    @make_call_method([W_RootStruct, values.W_Object], simple=False)
    def call(self, struct, val, env, cont):
        return self.mutator.mutate(struct, self.field, val, env, cont)

    def tostring(self):
        return "#<procedure:%s-%s!>" % (self.mutator.type.name, self.field_name)

class W_StructMutator(values.W_Procedure):
    errorname = "struct-mutator"
    _immutable_fields_ = ["type"]
    def __init__ (self, type):
        self.type = type

    def mutate(self, struct, field, val, env, cont):
        return struct.set(self.type, field.value, val, env, cont)

    call = label(make_call_method([W_RootStruct, values.W_Fixnum, values.W_Object], simple=False)(mutate))

    def tostring(self):
        return "#<procedure:%s-set!>" % self.type.name

class W_StructProperty(values.W_Object):
    errorname = "struct-type-property"
    _immutable_fields_ = ["name", "guard", "supers", "can_imp"]
    def __init__(self, name, guard, supers=values.w_null, can_imp=False):
        self.name = name.value
        self.guard = guard
        self.supers = values.from_list(supers)
        self.can_imp = can_imp

    def isinstance(self, prop):
        if self is prop:
            return True
        for super in self.supers:
            if super.car().isinstance(prop):
                return True
        return False

    def tostring(self):
        return "#<struct-type-property:%s>"%self.name

w_prop_procedure = W_StructProperty(values.W_Symbol.make("prop:procedure"), values.w_false)
w_prop_checked_procedure = W_StructProperty(values.W_Symbol.make("prop:checked-procedure"), values.w_false)
w_prop_arity_string = W_StructProperty(values.W_Symbol.make("prop:arity-string"), values.w_false)
w_prop_custom_write = W_StructProperty(values.W_Symbol.make("prop:custom-write"), values.w_false)
w_prop_equal_hash = W_StructProperty(values.W_Symbol.make("prop:equal+hash"), values.w_false)
w_prop_chaperone_unsafe_undefined = W_StructProperty(values.W_Symbol.make("prop:chaperone-unsafe-undefined"), values.w_false)

class W_StructPropertyPredicate(values.W_Procedure):
    errorname = "struct-property-predicate"
    _immutable_fields_ = ["property"]
    def __init__(self, prop):
        self.property = prop
    @make_call_method([values.W_Object])
    def call(self, arg):
        if not isinstance(arg, W_RootStruct):
            return values.w_false
        props = arg.struct_type().props
        for p, val in props:
            if p is self.property:
                return values.w_true
        return values.w_false

class W_StructPropertyAccessor(values.W_Procedure):
    errorname = "struct-property-accessor"
    _immutable_fields_ = ["property"]
    def __init__(self, prop):
        self.property = prop
    @label
    @make_call_method([values.W_Object], simple=False)
    def call(self, arg, env, cont):
        from pycket.interpreter import return_value
        if isinstance(arg, W_RootStruct):
            return arg.get_prop(self.property, env, cont)
        raise SchemeException("%s-accessor: expected %s? but got %s" %
                (self.property.name, self.property.name, arg.tostring()))

def struct2vector(struct, immutable=False):
    struct_desc = struct.struct_type().name
    first_el = values.W_Symbol.make("struct:" + struct_desc)
    return values_vector.W_Vector.fromelements([first_el] + struct.vals(), immutable=immutable)
