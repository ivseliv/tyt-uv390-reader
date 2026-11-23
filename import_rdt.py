"""Read public settings from TYT MD-UV380 and MD-UV390 CPS files."""

import os
from pathlib import Path

from cps_basic import basic_section
from cps_dtmf import dtmf_section, sequence_section
from cps_sections import rdt_sections
from read_settings import REGIONS, decode_regions


# TYT CPS container offsets from qdmr's UV390FileReader and editcp's MD-UV390 layout.
RDT_SIZE = 852533
MODEL_OFFSET = 0x125
# (radio start, radio end exclusive, corresponding offset in the CPS file)
SEGMENTS = ((0x002000, 0x040000, 0x002225), (0x110000, 0x1A0000, 0x040235))


def import_rdt(path):
    # Read only the model and the public ranges used by the USB reader.
    with Path(path).open("rb", buffering=0) as source:
        if os.fstat(source.fileno()).st_size != RDT_SIZE:
            raise ValueError("Неизвестный размер RDT. Для TYT MD-UV380/390 ожидается 852 533 байта.")
        source.seek(MODEL_OFFSET)
        model = source.read(8)
        if model not in (b"MD-UV380", b"MD-UV390"):
            raise ValueError("Этот RDT не содержит заголовок TYT MD-UV380 или MD-UV390.")
        regions = []
        for start, end in REGIONS:
            for first, limit, offset in SEGMENTS:
                if first <= start < end <= limit:
                    source.seek(offset + start - first)
                    block = source.read(end - start)
                    if len(block) != end - start:
                        raise ValueError("Файл RDT обрывается внутри настроек.")
                    regions.append(block)
                    break
            else:
                raise ValueError("Запрошенная область отсутствует в разметке RDT.")
        report = {"file_model": model.decode("ascii"), **decode_regions(regions, include_csv=True)}
        def read_at(offset, size):
            source.seek(offset)
            return source.read(size)
        sections = rdt_sections(read_at, report)
        info = basic_section(read_at, report["file_model"])
        if info:
            sections.insert(0, info)
        dtmf = dtmf_section(read_at)
        if dtmf:
            sections.append(dtmf)
        sections.append(sequence_section(read_at))
        report["cps_sections"] = sections
        return report
