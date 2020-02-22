<template>
  <div class="uploadBox" @click="fileChange">
    <input
      class="upload"
      ref="uploadFile"
      type="file"
      :disabled="disable"
      @change="upload($event)"
    />
    <slot></slot>
    <div class="progress-mask" v-if="isProgress" @click.stop="maskClick">
      <Progress class="progress" :percent="percent" hide-info />
    </div>
  </div>
</template>
<script>
import SparkMD5 from 'spark-md5'
const qiniu = require('qiniu-js')
export default {
  props: {
    disable: Boolean,
  },
  data() {
    return {
      token: '',
      key: '',
      percent: 0,
      isProgress: false,
      timerId: '',
      imgUrl: '',
    }
  },
  watch: {},
  computed: {},
  mounted() {},
  beforeDestroy() {
    window.clearTimeout(this.timerId)
  },
  methods: {
    upload(e) {
      let file = e.target.files[0]
      if (file === undefined) return
      if (file.size >= 20971520) {
        return this.$Message.info({ content: '图片过大，请上传到图片空间' })
      }
      var fileReader = new FileReader()
      fileReader.readAsBinaryString(file)
      var fileMd5 = ''
      fileReader.onload = e => {
        if (file.size == e.total) {
          fileMd5 = SparkMD5.hashBinary(e.target.result)
          this.isProgress = true
          this.$api
            .custToken({ md5Code: fileMd5, fileName: file.name })
            .then(res => {
              if (res.returnCode === '0000') {
                this.token = res.data.token
                this.key = res.data.key
                let that = this
                let putExtra = {
                    fname: file.name,
                    params: {},
                    mimeType: ['image/png', 'image/jpeg', 'image/gif'],
                  },
                  config = {
                    useCdnDomain: true,
                    region: null,
                    checkByMD5: true,
                    forceDirect: true,
                  }
                // let name = file.name.split(".");
                this.key = this.key
                // 上传图片数据到七牛云
                let observable = qiniu.upload(
                  file,
                  this.key,
                  this.token,
                  putExtra,
                  config,
                )
                let observer = {
                  next(result) {
                    that.percent = result.total.percent
                    if (that.percent === 100) {
                      that.timerId = window.setTimeout(() => {
                        that.isProgress = false
                      }, 500)
                    }
                  },
                  error(err) {
                    console.log(err)
                    that.isProgress = false
                    that.$Message.error({
                      content: '上传失败,文件类可能不匹配',
                    })
                  },
                  complete() {
                    that.imgUrl = handleImgLinkCust(that.key)
                    let obj = {
                      name: file.name,
                      onlineName: that.key,
                      url: that.imgUrl,
                    }
                    that.$emit('uploadSuccess', obj)
                    // 解决连续上传同一张图片无法触发change事件问题
                    that.$refs.uploadFile.value = ''
                  },
                }
                /* eslint-disable */
                let subscription = observable.subscribe(observer)
              } else {
                this.$Message.error({ content: res.returnMsg })
              }
            })
        }
      }
    },
    fileChange() {
      this.$refs.uploadFile.click()
    },
    maskClick() {},
    handleImgLinkCust(data) {
      const baseLinkCust = 'http://qn.custphoto.excelll.com/' // 图片空间地址
      const thumbImg = '-preview'
      return baseLinkCust + data + thumbImg
    },
  },
}
</script>
<style lang="scss" scoped>
@import '@/assets/css/mixin.scss';
.uploadBox {
  width: 100%;
  height: 100%;
  position: relative;
  .upload {
    display: none;
  }
  .progress-mask {
    width: 100%;
    height: 100%;
    position: absolute;
    left: 0;
    top: 0;
    background: rgba(0, 0, 0, 0.5);
    z-index: 2;
    display: flex;
    flex: row nowrap;
    justify-content: center;
    align-items: center;
  }
  .progress {
    padding: 0 20px;
  }
}
</style>
