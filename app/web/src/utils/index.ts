/*
 *  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
 *
 *  Licensed under the Apache License, Version 2.0 (the "License");
 *  you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 *  See the License for the specific language governing permissions and
 *  limitations under the License.
 */

/**
 * @param  {String}  url
 * @param  {Boolean} isNoCaseSensitive
 * @return {Object}
 */
// import numeral from 'numeral';

import { Base64 } from 'js-base64';
import JSEncrypt from 'jsencrypt';

export const getWidth = () => {
  return { width: window.innerWidth };
};
export const rsaPsw = (password: string) => {
  const pub =
    '-----BEGIN PUBLIC KEY-----MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAhsCn30rijx1TyGRngZcxOP9JBntbqSU4nktPomQILCQn2H4PHnMQiTg37SYlUP1JQ/YMrODJTLasp0HH0mqkUeODKqKnMsd1IGzRfQ78K6APPXBVHJGND3OCHSqSMIFk89DPDq9ORO35ucJXWwstr5Cm5agWU5Jn6bf/RvIRlLN33XTqkg8PMjVnwX4fQwhoTPkPOhmM622ADTbH77hiBZJbTD2WRP9CjAts8hwRYrGR5jjW9pjKjZP7wxQh8DoGpmmsqTz+TGDGaFYMdT1/cDuFaafMdwLexJILsxdjPXwtljIwkdp3YFlFfSL2RsnWaLFTRQ5EuxR22l57wamWyQIDAQAB-----END PUBLIC KEY-----';
  const encryptor = new JSEncrypt();

  encryptor.setPublicKey(pub);

  return encryptor.encrypt(Base64.encode(password));
};

export default {
  getWidth,
  rsaPsw,
};

export const getFileExtension = (filename: string) =>
  filename.slice(filename.lastIndexOf('.') + 1).toLowerCase();
