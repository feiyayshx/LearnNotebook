<template>
  <div class="password-container">
    <input ref="pwd"
           type="tel"
           maxlength="6"
           autofocus
           v-model="password"
           style="position: absolute;z-index: -1;left:-100%;opacity: 0" />
    <ul class="pwd-wrap"
        @click="focus">
      <li><i v-if="valLength > 0"></i></li>
      <li><i v-if="valLength > 1"></i></li>
      <li><i v-if="valLength > 2"></i></li>
      <li><i v-if="valLength > 3"></i></li>
      <li><i v-if="valLength > 4"></i></li>
      <li><i v-if="valLength > 5"></i></li>
    </ul>
  </div>
</template>
<script>
export default {
  name: "password",
  props: {
    value: {
      type: [String, Number],
      default: ""
    }
  },
  data() {
    return {
      password: "",
      valLength: 0
    };
  },
  created() {
    this.password = String(this.value);
    this.valLength = this.password.length;
  },
  computed: {},
  watch: {
    value(val, oldVal) {
      if (val != oldVal) {
        this.password = val;
      }
    },
    password(curVal, oldval) {
      if (curVal !== oldval) {
        this.valLength = curVal.length;
        this.$emit("input", curVal);
        this.$emit("changeItem", curVal);
      }
    }
  },
  methods: {
    focus() {
      this.$refs.pwd.focus();
    }
  }
};
</script>

<style lang="scss" scoped>
.password-container {
  position: relative;
}
.pwd-wrap {
  width: 300px;
  height: 50px;
  margin-left: 25px;
  background: #fcfcfc;
  border: 1px solid #e6e6e6;
  display: flex;
  cursor: pointer;
}
.pwd-wrap li {
  width: 50px;
  height: 50px;
  line-height: 50px;
  box-sizing: border-box;
  list-style-type: none;
  text-align: center;
  flex: 1;
  border-right: 1px solid #e6e6e6;
}
.pwd-wrap li:last-child {
  border-right: 0;
}
.pwd-wrap li i {
  height: 10px;
  width: 10px;
  border-radius: 50%;
  background: #000;
  display: inline-block;
}
</style>
