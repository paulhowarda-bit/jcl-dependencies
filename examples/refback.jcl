//REFBACK  JOB (OPS),'SYMBOLS AND REFERBACKS',CLASS=A
//*
//* Two ways a DD names its dataset without spelling it out.
//*
//* A symbol's value coded in apostrophes - HLQ='PROD', GEN='+1' - holds
//* what is between them; the apostrophes delimit it. SORTIN is
//* PROD.SALES.DAILY, and SYSUT2 is PROD.SALES.ARCHIVE, generation +1.
//*
//* A backward reference - DSN=*.stepname.ddname - points at the dataset
//* an earlier DD names. Inside SORTPROC, REPORT reads what SORT wrote, by
//* the PROC's own step name; the job's ARCHIVE step reaches the same DD
//* as *.NIGHTLY.SORT.SORTOUT. Both bind PROD.SALES.SORTED, so SORT feeds
//* REPORT and ARCHIVE.
//*
//         SET GEN='+1'
//SORTPROC PROC HLQ='PROD'
//SORT     EXEC PGM=SORT
//SORTIN   DD  DSN=&HLQ..SALES.DAILY,DISP=SHR
//SORTOUT  DD  DSN=&HLQ..SALES.SORTED,DISP=(NEW,PASS)
//REPORT   EXEC PGM=SALESRPT
//INFILE   DD  DSN=*.SORT.SORTOUT,DISP=(OLD,PASS)
//RPT      DD  SYSOUT=*
//         PEND
//*
//NIGHTLY  EXEC SORTPROC
//ARCHIVE  EXEC PGM=IEBGENER
//SYSUT1   DD  DSN=*.NIGHTLY.SORT.SORTOUT,DISP=(OLD,DELETE)
//SYSUT2   DD  DSN=PROD.SALES.ARCHIVE(&GEN),DISP=(NEW,CATLG,DELETE)
//SYSIN    DD  DUMMY
//SYSPRINT DD  SYSOUT=*
//
