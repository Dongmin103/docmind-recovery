"""Create a non-sensitive DOCX acceptance fixture, without encryption or upload."""
import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED


def create(path):
    if path.suffix.lower() != '.docx':
        raise ValueError('Output must be .docx')
    # Exclusive creation: never overwrite a user's document.
    with path.open('xb') as target, ZipFile(target,'w',ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml','''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>''')
        archive.writestr('_rels/.rels','''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>''')
        archive.writestr('word/document.xml','''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>DocMind 엔진 인수 확인용 합성 문서</w:t></w:r></w:p><w:p><w:r><w:t>확인 표식은 DOCMIND-HANDOFF-20261110 입니다. 이 문서는 기밀이나 실제 고객 자료를 포함하지 않습니다. 문서 읽기, 청크 생성, 임베딩, 색인 활성화와 임시 파일 정리를 확인합니다.</w:t></w:r></w:p><w:sectPr/></w:body></w:document>''')
    return {'file':str(path),'bytes':path.stat().st_size,'plaintext_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'marker':'DOCMIND-HANDOFF-20261110','encrypted':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    print(json.dumps(create(parser.parse_args().output),ensure_ascii=False,indent=2))
