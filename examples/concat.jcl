//CONCAT   JOB (OPS),'CONCATENATED DDS',CLASS=A
//*
//* A named DD followed by unnamed ones is a concatenation: ONE ddname
//* reading SEVERAL datasets in turn. Every one of them is a dataset the
//* step reads, in the order coded.
//*
//* MERGE's SORTIN reads three. The third is allocated OLD, which says
//* how it is held, not that MERGE writes it. COPY1's SYSUT1 points back
//* at that SORTIN, and a backward reference to a concatenation is its
//* first dataset only.
//*
//* LOADPROC's RUN step concatenates two extracts on INFILE. The job
//* overrides the SECOND only: the DD with nothing on it leaves the first
//* as the PROC coded it.
//*
//* PRINT's SYSUT1 is defined by a later DD - DDNAME=FEED - and FEED is
//* itself a concatenation. SYSUT1 takes FEED's first dataset; the second
//* is concatenated to the last DD statement before FEED, which here is
//* SYSUT2 and not SYSUT1. That is what the system does.
//*
//         SET HLQ=PROD
//LOADPROC PROC
//RUN      EXEC PGM=SALESLD
//INFILE   DD  DSN=&HLQ..SALES.EXTRACT.EAST,DISP=SHR
//         DD  DSN=&HLQ..SALES.EXTRACT.WEST,DISP=SHR
//REPORT   DD  SYSOUT=*
//         PEND
//*
//MERGE    EXEC PGM=SORT
//SORTIN   DD  DSN=&HLQ..SALES.DAILY.MON,DISP=SHR
//         DD  DSN=&HLQ..SALES.DAILY.TUE,DISP=SHR
//*        a comment between them does not end the concatenation
//         DD  DSN=&HLQ..SALES.DAILY.WED,DISP=OLD
//SORTOUT  DD  DSN=&HLQ..SALES.WEEK,
//             DISP=(NEW,CATLG,DELETE)
//SYSIN    DD  *
  SORT FIELDS=(1,10,CH,A)
/*
//COPY1    EXEC PGM=IEBGENER
//SYSUT1   DD  DSN=*.MERGE.SORTIN,DISP=SHR
//SYSUT2   DD  DSN=&HLQ..SALES.MON.COPY,DISP=(NEW,CATLG,DELETE)
//SYSIN    DD  DUMMY
//SYSPRINT DD  SYSOUT=*
//LOAD     EXEC LOADPROC
//RUN.INFILE DD
//         DD  DSN=&HLQ..SALES.EXTRACT.WEST.RERUN
//PRINT    EXEC PGM=IEBGENER
//SYSUT1   DD  DDNAME=FEED
//SYSUT2   DD  SYSOUT=*
//FEED     DD  DSN=&HLQ..SALES.WEEK,DISP=SHR
//         DD  DSN=&HLQ..SALES.WEEK.PRIOR,DISP=SHR
//SYSIN    DD  DUMMY
//SYSPRINT DD  SYSOUT=*
