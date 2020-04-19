module.exports = {
  mode: 'development',
  entry: './src/index.js',
  outpath: {
    path: path.resolve[(__dirname, 'dist')],
    fileName: '',
  },
  noParse: /jquery/,  // 如果知道该库没有依赖，不去解析该库中的依赖库
  modules: {
    rules: [
      {
        test: /\.js$/,
        exclude: /node_modules/,
        include:'', //,
        use: {
          loader: 'babel-loader',
          options: {
            presets: ['@babel/preset-env'],
          },
        },
      },
    ],
  },
  resolve: {
    // 解析第三方包
    modules: [path.resolve('node_modules')],
    //
    alias: {},
  },
}
